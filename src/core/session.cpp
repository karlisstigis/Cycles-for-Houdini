// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include "render_devices.h"
#include <OpenImageIO/filesystem.h>

namespace cycles_houdini {
namespace {
std::mutex initialization_mutex;
bool initialized{};
std::string initialized_root;
// Explicit in-process reference sessions share stock Cycles' OCIO globals.
// This lock only admits/releases their policy identities; it is never held
// across rendering, cancellation, cache work, or any other session's control.
std::mutex color_admission_mutex;
std::map<const RenderSession *, std::string> color_admissions;
void admit_color(const RenderSession *owner, const std::string &identity) {
  std::lock_guard lock(color_admission_mutex);
  for (const auto &[peer, policy] : color_admissions)
    if (peer != owner && policy != identity)
      throw SceneUpdateRejected("Concurrent in-process sessions have conflicting OCIO configurations. "
                                "Use execution='worker' for sessions with independent color configurations, "
                                "or close the other sync session before changing its configuration.");
  color_admissions[owner] = identity;
}
void release_color(const RenderSession *owner) {
  std::lock_guard lock(color_admission_mutex);
  color_admissions.erase(owner);
}
std::shared_ptr<std::filesystem::path> make_tile_storage() {
  for (int attempt = 0; attempt < 16; ++attempt) {
    const auto directory = std::filesystem::temp_directory_path() /
                           OIIO::Filesystem::unique_path("cycles-houdini-tiles-%%%%-%%%%-%%%%-%%%%");
    if (std::filesystem::create_directory(directory))
      return {new std::filesystem::path(directory), [](std::filesystem::path *path) {
                // Only this exclusively-created session directory is removed.
                // Also retires unfinished EXRs after cancellation or an engine error.
                std::error_code error;
                std::filesystem::remove_all(*path, error);
                delete path;
              }};
  }
  throw std::runtime_error("Cannot reserve a temporary render-tile directory");
}
} // namespace

void initialize(const std::string &resource_root, const std::string &user_cache_root) {
  std::lock_guard lock(initialization_mutex);
  if (initialized) {
    if (resource_root != initialized_root)
      throw std::logic_error("Cycles resources are already initialized at another path");
    return;
  }
  if (!std::filesystem::is_directory(resource_root))
    throw std::invalid_argument("Missing Cycles resource directory: " + resource_root);
  // Worker stdout carries the binary protocol, including during profiling.
  // Keep all engine logging on stderr even when verbose tracing is enabled.
  ccl::log_init([](ccl::LogLevel level, const char *, const char *, const char *message) {
    std::fprintf(stderr, "%s\n", message);
    if (level == ccl::LOG_LEVEL_FATAL || level == ccl::LOG_LEVEL_DFATAL)
      std::abort();
  });
  if (const auto *trace = std::getenv("CYCLES_HOUDINI_TRACE_KERNELS"); trace && std::string_view(trace) == "1")
    ccl::log_level_set(ccl::LOG_LEVEL_TRACE);
  ccl::path_init(resource_root, user_cache_root);
  initialized = true;
  initialized_root = resource_root;
}
Capabilities capabilities() {
  Capabilities result;
  result.image_iris = image_iris_available();
#ifdef WITH_PATH_GUIDING
  result.path_guiding = true;
#endif
#ifdef WITH_BDPT_PROTOTYPE
  result.bdpt_prototype = true;
#endif
  result.engine_commit = CH_ENGINE_UPSTREAM;
#if defined(WITH_OPENVDB) && defined(WITH_NANOVDB)
  result.volumes = true;
#endif
  for (const auto &d : ccl::Device::available_devices())
    result.devices.push_back({d.id, d.description, ccl::Device::string_from_type(d.type), d.display_device,
                              available_denoisers(d), physical_device_id(d)});
  return result;
}
void validate_object_properties(const ObjectProperties &p) {
  validate_pass_index(p.pass_index);
  validate_dielectric_priority(p.dielectric_priority);
  for (const auto value : {p.shadow_terminator_shading, p.shadow_terminator_geometry})
    if (!std::isfinite(value) || value < 0 || value > 1)
      throw std::invalid_argument("Shadow terminator offsets must be finite and within 0..1");
  if (!std::isfinite(p.ao_distance) || p.ao_distance < 0)
    throw std::invalid_argument("Object AO distance must be finite and nonnegative");
  if (!std::isfinite(p.color.x) || !std::isfinite(p.color.y) || !std::isfinite(p.color.z) || !std::isfinite(p.alpha))
    throw std::invalid_argument("Object colour must be finite");
  validate_lightgroup_assignment(p.lightgroup);
  validate_cryptomatte_name(p.cryptomatte_object);
  validate_cryptomatte_name(p.cryptomatte_asset);
  if (p.linking_id.size() > 4096 || p.linking_id.find('\0') != std::string::npos)
    throw std::invalid_argument("Invalid object linking identity");
}

void validate_dielectric_priority(int priority) {
  if (priority < -dielectric_priority_limit || priority > dielectric_priority_limit)
    throw std::invalid_argument("Dielectric Priority requires an integer within -16777216..16777216");
}

void RenderSession::Impl::invalidate_engine_color(std::uint64_t next_epoch) {
  if (!session)
    return;
  bool was_complete;
  {
    std::lock_guard frame_lock(store->mutex);
    was_complete = store->complete;
  }
  cancel_engine();
  color_update_pending = true;
  target_color_epoch = next_epoch;
  std::lock_guard frame_lock(store->mutex);
  if (state == SessionState::Rendering)
    state = was_complete ? SessionState::Complete : SessionState::Idle;
  if (generation && store->finished_seconds == 0)
    store->finished_seconds = seconds(store->started);
}

bool RenderSession::Impl::motion_vectors() const {
  return std::find(config.passes.begin(), config.passes.end(), "motion") != config.passes.end();
}

bool RenderSession::Impl::sampled_transforms() const {
  return session->scene->integrator->get_motion_blur() || motion_vectors();
}

bool RenderSession::Impl::interactive_denoising_motion() const {
  return config.interactive && denoise_policy.backend == "dlss" && !sampled_transforms();
}

ccl::array<ccl::Transform> RenderSession::Impl::denoising_motion(const ccl::array<ccl::Transform> &previous,
                                                                 const ccl::Transform &current) const {
  return denoising_motion_history(previous, current);
}

void RenderSession::Impl::ensure_open() const {
  if (!session)
    throw std::logic_error("Render session is closed");
}

DenoisePolicy RenderSession::Impl::resolve_session_denoising(const DenoiseSettings &settings,
                                                             const std::vector<std::string> *passes,
                                                             std::optional<bool> motion_blur) const {
  if (settings.enabled && settings.method == "dlss" && !config.interactive)
    throw std::invalid_argument(
        "DLSS is available only for interactive rendering; select OIDN or OptiX for final output");
  auto effective = settings;
  const auto &requested = passes ? *passes : config.passes;
  const bool authored_vectors = std::find(requested.begin(), requested.end(), "motion") != requested.end();
  const bool blur = motion_blur.value_or(session->scene->integrator->get_motion_blur());
  const bool conventional = settings.enabled && settings.method == "dlss" && (authored_vectors || blur);
  if (conventional) {
    effective.method = "auto";
    effective.upscale_factor = 1.0f;
  }
  auto policy = resolve_denoising(effective, params.device);
  if (conventional)
    policy.warning = "DLSS requires interactive motion history. Using the Automatic denoiser while explicit Motion "
                     "Vectors or shutter motion blur is enabled; authored motion data is preserved.";
  if (policy.parameters.use && policy.parameters.use_gpu) {
    // Query the same public engine selection used by PathTrace. Keep
    // the render MultiDevice for denoising, reusing its GPU contexts.
    ccl::Device *selected = nullptr;
    const auto effective = ccl::get_effective_denoise_params(session->device.get(), nullptr, policy.parameters,
                                                             ccl::GraphicsInteropDevice{}, selected);
    if (!selected || !effective.use_gpu || effective.type != policy.parameters.type)
      throw std::invalid_argument(
          "Requested GPU denoiser is unavailable on the selected render devices; no CPU fallback is applied");
    policy.device_id = selected->info.id;
  }
  return policy;
}

void RenderSession::Impl::configure_denoising(const DenoiseSettings &settings, const DenoisePolicy &policy,
                                              bool force_passes) {
  auto *scene = session->scene.get();
  auto *integrator = scene->integrator;
  const auto &params = policy.parameters;
  const bool passes_changed = force_passes || store->passes.empty() || config.denoising.enabled != settings.enabled;
  integrator->set_use_denoise(params.use);
  integrator->set_denoiser_type(params.type);
  integrator->set_denoise_use_gpu(params.use_gpu);
  integrator->set_denoise_start_sample(params.start_sample);
  integrator->set_denoiser_passes(params.passes);
  integrator->set_denoiser_quality(params.quality);
  integrator->set_denoiser_prefilter(params.prefilter);
  integrator->set_denoiser_upscale_factor(params.upscale_factor);
  integrator->set_use_pixel_jitter(params.use && params.type == ccl::DENOISER_DLSS);
  integrator->tag_update(scene, ccl::Integrator::UPDATE_ALL);
  if (passes_changed) {
    ccl::set<ccl::Pass *> retired;
    for (auto *pass : scene->passes)
      retired.insert(pass);
    scene->delete_nodes(retired);
    auto names = config.passes;
    if (settings.enabled)
      for (const char *name : {"noisy_combined", "denoising_albedo", "denoising_normal"})
        if (std::find(names.begin(), names.end(), name) == names.end())
          names.push_back(name);
    // Explicit animation vectors must not use Cycles viewport-history buffers.
    scene->params.background = !config.interactive || std::find(names.begin(), names.end(), "motion") != names.end();
    const bool separate_catcher = std::find(names.begin(), names.end(), "shadow_catcher") != names.end();
    // Match Cycles' two compositing workflows: approximate shadows in
    // Beauty, or an explicit RGB multiplier plus foreground RGBA.
    scene->film->set_use_approximate_shadow_catcher(!separate_catcher);
    scene->film->tag_modified();
    std::vector<PassDescriptor> descriptors;
    const auto catalogue = pass_catalogue();
    for (const auto &id : names) {
      const auto entry = std::find_if(catalogue.begin(), catalogue.end(), [&](const auto &p) { return p.id == id; });
      descriptors.push_back(*entry);
      descriptors.back().internal = std::find(config.passes.begin(), config.passes.end(), id) == config.passes.end();
      // Same-mode Combined buffers are not independent engine passes.
      // Alias raw beauty when denoising is off rather than allocating an unwritten buffer.
      if (id == "noisy_combined" && !settings.enabled)
        continue;
      auto *pass = scene->create_node<ccl::Pass>();
      pass->set_type(pass_type(id));
      pass->set_name(ccl::ustring(id));
      const bool denoised = settings.enabled && (id == "combined" || id == "shadow_catcher");
      pass->set_mode(denoised ? ccl::PassMode::DENOISED : ccl::PassMode::NOISY);
    }
    for (const auto &aov : config.aovs) {
      auto *pass = scene->create_node<ccl::Pass>();
      pass->set_type(aov.color ? ccl::PASS_AOV_COLOR : ccl::PASS_AOV_VALUE);
      pass->set_name(ccl::ustring(aov.name));
      pass->set_mode(ccl::PassMode::NOISY);
    }
    const auto custom = aov_descriptors(config.aovs);
    descriptors.insert(descriptors.end(), custom.begin(), custom.end());
    for (const auto &name : config.lightgroups) {
      auto *pass = scene->create_node<ccl::Pass>();
      pass->set_type(ccl::PASS_COMBINED);
      pass->set_name(ccl::ustring("lightgroup_" + name));
      pass->set_lightgroup(ccl::ustring(name));
      pass->set_mode(ccl::PassMode::NOISY);
    }
    const auto grouped = lightgroup_descriptors(config.lightgroups);
    descriptors.insert(descriptors.end(), grouped.begin(), grouped.end());
    const auto crypto = cryptomatte_descriptors(config.cryptomatte);
    scene->film->set_cryptomatte_depth((config.cryptomatte.levels + 1) / 2);
    const auto flags = (config.cryptomatte.object ? ccl::CRYPT_OBJECT : 0) |
                       (config.cryptomatte.material ? ccl::CRYPT_MATERIAL : 0) |
                       (config.cryptomatte.asset ? ccl::CRYPT_ASSET : 0);
    scene->film->set_cryptomatte_passes(static_cast<ccl::CryptomatteType>(flags));
    scene->film->tag_modified();
    for (const auto &descriptor : crypto) {
      auto *pass = scene->create_node<ccl::Pass>();
      pass->set_type(ccl::PASS_CRYPTOMATTE);
      pass->set_name(ccl::ustring(descriptor.id));
      pass->set_mode(ccl::PassMode::NOISY);
    }
    descriptors.insert(descriptors.end(), crypto.begin(), crypto.end());
    std::lock_guard lock(store->mutex);
    store->passes = std::move(descriptors);
    if (store->preview_pass != "*" && std::none_of(store->passes.begin(), store->passes.end(),
                                                   [&](const auto &p) { return p.id == store->preview_pass; }))
      store->preview_pass = "combined";
  }
  config.denoising = settings;
  denoise_policy = policy;
  std::lock_guard lock(store->mutex);
  store->denoiser = policy.backend;
  store->denoise_device_id = policy.device_id;
}

bool RenderSession::Impl::pending_camera_tessellation() const {
  bool pending = false;
  for (const auto &[key, record] : meshes) {
    if (!record.render || record.sharing_family->leader != key)
      continue;
    for (const auto &entry : record.render->subdivision_metrics) {
      const auto &metric = entry.second;
      std::lock_guard lock(metric->mutex);
      if (!metric->ready) {
        // Do not interrupt unfinished geometry independent of the camera.
        if (!record.pixel_adaptive_subdivision())
          return false;
        pending = true;
      }
    }
  }
  return pending;
}

void RenderSession::Impl::cancel_engine() {
  const auto started = Clock::now();
  ++session_cancel_calls;
  std::uint64_t outputs{}, displays{}, skipped_outputs{}, skipped_displays{};
  double output_seconds{}, display_seconds{};
  {
    std::lock_guard frame_lock(store->mutex);
    store->interrupted = true;
    outputs = store->diagnostics.output_deliveries;
    displays = store->diagnostics.display_updates;
    skipped_outputs = store->diagnostics.output_reads_skipped;
    skipped_displays = store->diagnostics.display_updates_skipped;
    output_seconds = store->diagnostics.output_delivery_seconds;
    display_seconds = store->diagnostics.display_conversion_seconds;
  }
  session->cancel(true);
  cancel_join_seconds = seconds(started);
  {
    std::lock_guard frame_lock(store->mutex);
    cancel_output_deliveries = store->diagnostics.output_deliveries - outputs;
    cancel_display_updates = store->diagnostics.display_updates - displays;
    cancel_output_reads_skipped = store->diagnostics.output_reads_skipped - skipped_outputs;
    cancel_display_updates_skipped = store->diagnostics.display_updates_skipped - skipped_displays;
    cancel_output_delivery_seconds = store->diagnostics.output_delivery_seconds - output_seconds;
    cancel_display_conversion_seconds = store->diagnostics.display_conversion_seconds - display_seconds;
  }
}

void RenderSession::Impl::set_topology_cache_limit(std::uint32_t mib) {
  if (config.interactive)
    session->scene->geometry_manager->set_subdivision_osd_cache_bytes(std::size_t(mib) * 1024 * 1024);
}

RenderSession::Impl::Impl(const SessionConfig &c, std::shared_ptr<const ColorConfiguration> color)
    : config(c), crop{0, 0, c.width, c.height} {
  if (!initialized)
    throw std::logic_error("Call initialize before creating a session");
  source_color = target_color = std::move(color);
  source_color_epoch = target_color_epoch = 1;
  if (c.width < 1 || c.height < 1 || c.samples < 1 || c.threads < 0)
    throw std::invalid_argument("Invalid render dimensions/samples/threads");
  if (c.tile_size < 1)
    throw std::invalid_argument("Render tile size must be positive");
  if (c.hair_subdivisions < 0 || c.hair_subdivisions > 24)
    throw std::invalid_argument("Curve Subdivisions must be 0 to 24");
  // Cycles indexes pass buffers with signed integers. Reject overflow before allocating.
  const auto pixels = std::uint64_t(c.width) * c.height;
  validate_pass_outputs(c.aovs, c.lightgroups);
  validate_cryptomatte(c.cryptomatte);
  const auto components =
      std::max<std::uint64_t>(4, std::uint64_t(c.passes.size() + c.aovs.size() + c.lightgroups.size() +
                                               cryptomatte_descriptors(c.cryptomatte).size()) *
                                         4 +
                                     32);
  if (pixels > std::uint64_t(std::numeric_limits<int>::max()) / components)
    throw std::invalid_argument("Render dimensions exceed Cycles' safe pass-buffer indexing range");
  params.device = resolve_render_devices(c, render_device_ids);
  if (c.denoising.enabled && c.denoising.method == "dlss" && !c.interactive)
    throw std::invalid_argument(
        "DLSS is available only for interactive rendering; select OIDN or OptiX for final output");
  params.denoise_device = params.device;
  params.samples = c.samples;
  params.threads = c.threads;
  // Headless disables Cycles' display scheduler even with a DisplayDriver attached.
  // Our CPU-buffer display does not require a graphics context.
  params.headless = !(c.interactive || c.progressive_display);
  params.background = !c.interactive;
  params.use_gpu_concurrent_states_growth = !c.interactive;
  params.use_auto_tile = c.use_tiling && !c.interactive;
  params.tile_size = c.tile_size;
  if (params.use_auto_tile) {
    tile_storage = make_tile_storage();
    const auto name = tile_storage->u8string();
    params.temp_dir.assign(name.begin(), name.end());
  }
  // Reuse Cycles' navigation scheduler: show a low-resolution sample
  // first and refine while idle. Final output always uses full buffers.
  params.use_resolution_divider = c.interactive;
  // A finite IPR sample budget can finish before Cycles tightens its
  // progressive noise floor. Keep display/navigation progressive, but
  // always judge pixel convergence against the requested threshold.
  params.use_progressive_noise_floor = false;
  ccl::SceneParams scene_params;
  scene_params.background = params.background;
  // Original mesh ownership and instance variants use object-space
  // buffers. Do not permit the static BVH path to bake host transforms.
  scene_params.bvh_type = ccl::BVH_TYPE_DYNAMIC;
  // ROP Curve Subdivisions (Blender's scene.cycles_curves.subdivisions, default 2; Cycles'
  // own SceneParams default is 3). It refines Rounded Ribbons only (ribbon kernel; Embree on the CPU).
  scene_params.hair_subdivisions = c.hair_subdivisions;
  scene_params.use_texture_cache = c.use_texture_cache;
  scene_params.auto_texture_cache = c.auto_texture_cache;
#ifdef WITH_BDPT_PROTOTYPE
  if (const char *experiment = std::getenv("CYCLES_HOUDINI_BDPT_PROTOTYPE");
      experiment && std::string(experiment) == "1") {
    // Experimental reciprocal shader evaluation currently requires resident images;
    // ordinary sessions retain their authored streaming/cache policy.
    scene_params.use_texture_cache = false;
    scene_params.auto_texture_cache = false;
  }
#endif
  session = std::make_unique<ccl::Session>(params, scene_params);
  set_topology_cache_limit(dicing_settings.topology_cache_mib);
  if (params.use_auto_tile)
    session->full_buffer_written_cb = [this](ccl::string_view filename) {
      // Stock Cycles spills multi-tile pass buffers to disk instead of calling
      // OutputDriver for individual completed tiles. Reload and denoise the
      // complete cropped frame on the render thread before publishing completion.
      // The scene stays resident for persistent final-render sequences. Rendering
      // buffers are tiled; host output and final denoising still use a full frame.
      try {
        bool interrupted;
        {
          std::lock_guard lock(store->mutex);
          interrupted = store->interrupted;
        }
        if (!interrupted && !session->progress.get_cancel())
          session->process_full_buffer_from_disk(filename);
      } catch (const std::exception &error) {
        store->fail(error.what());
        session->progress.set_error(error.what());
      }
      std::error_code error;
      std::filesystem::remove(std::filesystem::u8path(filename.begin(), filename.end()), error);
    };
  if (session->device->have_error())
    throw std::runtime_error(session->device->error_message());
  const auto requested = render_device_ids;
  render_device_ids.clear();
  session->device->foreach_device([&](ccl::Device *device) { render_device_ids.push_back(device->info.id); });
  std::sort(render_device_ids.begin(), render_device_ids.end());
  auto expected = requested;
  std::sort(expected.begin(), expected.end());
  if (render_device_ids != expected)
    throw std::runtime_error("Cycles did not instantiate every selected render device");
  store->diagnostics.enabled = c.collect_diagnostics;
  if (c.collect_diagnostics) {
    session->scene->enable_update_stats(false, true);
    session->stats.buffer_copy_callback = [frames = store](const char *name, std::size_t bytes, double duration) {
      std::lock_guard lock(frames->mutex);
      auto &entry = frames->diagnostics.buffer_copy_requests[name ? name : "unnamed"];
      ++entry.calls;
      entry.requested_bytes += bytes;
      entry.host_seconds += duration;
    };
  }
  // Cycles identifies the world through a BackgroundLight object.
  // Without it KernelBackground.object_index remains zero and the first
  // scene object can be mistaken for the world by volume classification.
  // Keep the normal world identity even for a black environment.
  auto *environment = session->scene->create_node<ccl::BackgroundLight>();
  environment->name = ccl::ustring("cycles_houdini/environment");
  environment->set_strength(ccl::make_float3(1));
  ccl::array<ccl::Node *> environment_shaders;
  environment_shaders.reserve(1);
  environment_shaders.push_back_reserved(session->scene->default_background);
  environment->set_used_shaders(environment_shaders);
  auto *environment_object = session->scene->create_node<ccl::Object>();
  background_object = environment_object;
  environment_object->name = ccl::ustring("cycles_houdini/environment");
  environment_object->set_geometry(environment);
  // As in Blender, real lights illuminate both the ordinary scene and
  // the unoccluded catcher reference. Otherwise division has no lighting
  // denominator and returns identity even where an actor casts a shadow.
  environment_object->set_is_shadow_catcher(true);
  environment_object->set_visibility(ccl::PATH_RAY_VISIBILITY_ALL & ~ccl::PATH_RAY_VISIBILITY_CAMERA);
  // The pinned engine's default_light graph intentionally emits zero.
  // LightData strength is multiplied by this shader, so use a neutral
  // unit emission graph for all integration-created numeric lights.
  auto light_graph = ccl::make_unique<ccl::ShaderGraph>();
  auto *emission = light_graph->create_node<ccl::EmissionNode>();
  emission->set_color(ccl::make_float3(1, 1, 1));
  emission->set_strength(1);
  light_graph->connect(emission->output("Emission"), light_graph->output()->input("Surface"));
  session->scene->default_light->set_graph(std::move(light_graph));
  session->scene->default_light->tag_update(session->scene.get());
  rendering.samples = c.samples;
#ifdef WITH_BDPT_PROTOTYPE
  if (const char *experiment = std::getenv("CYCLES_HOUDINI_BDPT_PROTOTYPE");
      experiment && std::string(experiment) == "1") {
    // No authored snapshot exists at construction. Establish a compatible empty
    // experiment; subsequent authored settings still undergo strict validation.
    rendering.light_tree = false;
    rendering.adaptive_sampling = false;
    rendering.fast_gi = false;
    rendering.max_bounce = rendering.diffuse_bounce = rendering.glossy_bounce = rendering.transmission_bounce = 7;
    rendering.filter_glossy = rendering.clamp_direct = rendering.clamp_indirect = 0;
    rendering.caustics_reflective = rendering.caustics_refractive = true;
  }
#endif
  validate_render_settings(rendering);
  configure_render_settings(rendering, *session, params, c.interactive);
  session->scene->film->set_display_pass(ccl::PASS_COMBINED);
  validate_passes(c.passes);
  validate_aovs(c.aovs);
  configure_denoising(c.denoising, resolve_session_denoising(c.denoising));
  session->set_output_driver(make_buffer_output(store));
  if (c.interactive || c.progressive_display)
    session->set_display_driver(make_buffer_display(store));
}

RenderSession::RenderSession(const SessionConfig &config) {
  synchronize_color_configuration();
  const auto color = current_color_configuration();
  admit_color(this, color->policy().render_policy_id);
  try {
    impl_ = std::make_unique<Impl>(config, color);
  } catch (...) {
    release_color(this);
    throw;
  }
}
RenderSession::~RenderSession() { close(); }
std::uint64_t RenderSession::apply(const SceneUpdate &incoming, bool reuse_if_unchanged) {
  std::lock_guard lock(impl_->control);
  auto &i = *impl_;
  i.ensure_open();
  const auto desired_color = current_color_configuration();
  admit_color(this, desired_color->policy().render_policy_id);
  if (desired_color->policy().render_policy_id != i.target_color->policy().render_policy_id) {
    if (!i.color_update_pending)
      i.invalidate_engine_color(i.target_color_epoch + 1);
    i.target_color = desired_color;
  }
  UpdateProfile profile(incoming);
  bool mutation_started = false;
  try {
    // The candidate owns displacement expansion only when necessary. Prepared
    // resources and borrowed command references live through synchronize_scene;
    // the engine boundary completes before apply returns.
    std::string attribute_fallback_reason;
    auto attribute_records = i.direct_attribute_records(incoming, attribute_fallback_reason);
    profile.mark("attribute_eligibility");
    auto attribute_expansion = i.expand_object_attributes(incoming, attribute_records ? &*attribute_records : nullptr);
    const SceneUpdate &source = attribute_expansion ? *attribute_expansion : incoming;
    profile.mark("attribute_expansion");
    auto expanded = i.expand_displacement_materials(source);
    const SceneUpdate &candidate_update = expanded ? *expanded : source;
    auto prepared = i.prepare_update(source, candidate_update, profile);
    std::optional<SceneUpdate> resource_expansion;
    std::optional<SceneUpdate> resource_displacement_expansion;
    if (!candidate_update.object_attributes.empty() &&
        (!prepared.prepared_graphs.empty() || !prepared.prepared_bindings.empty() ||
         !prepared.effective_materials.empty() || prepared.prepared_world || prepared.world_changed ||
         !prepared.volume_recovery.empty() || !prepared.binding_handoffs.meshes.empty() ||
         !prepared.binding_handoffs.shaders.empty() || prepared.aovs_changed || prepared.lightgroups_changed ||
         prepared.crypto_changed)) {
      // Shader/resource repair can change demands without authored material
      // DTOs. Expand the remaining direct subset and validate the full candidate
      // again before the engine boundary. Lights do not change object bindings.
      if (attribute_fallback_reason.empty())
        attribute_fallback_reason = "prepared_resources";
      if (profile.enabled)
        std::fprintf(stderr, "CH_ATTRIBUTE_FALLBACK reason=prepared_resources\n");
      resource_expansion = i.expand_object_attributes(candidate_update);
      resource_displacement_expansion = i.expand_displacement_materials(*resource_expansion);
      prepared = i.prepare_update(
          *resource_expansion, resource_displacement_expansion ? *resource_displacement_expansion : *resource_expansion,
          profile);
      attribute_records.reset();
    }
    const SceneUpdate &u = resource_displacement_expansion ? *resource_displacement_expansion
                           : resource_expansion            ? *resource_expansion
                                                           : candidate_update;
    if (attribute_records) {
      std::erase(*attribute_records, nullptr);
      prepared.attribute_records = std::move(*attribute_records);
      prepared.object_edits |= std::any_of(u.object_attributes.begin(), u.object_attributes.end(),
                                           [](const auto &value) { return value.pose.has_value(); });
    }
    prepared.expanded_attribute_updates = incoming.object_attributes.size() - u.object_attributes.size();
    if (prepared.expanded_attribute_updates)
      prepared.attribute_fallback_reason = std::move(attribute_fallback_reason);
    auto &force_dicing = prepared.force_dicing;
    auto &hold_dicing = prepared.hold_dicing;
    auto &dicing_projection_changed = prepared.dicing_projection_changed;
    auto &iris_changed = prepared.iris_changed;
    auto &aovs_changed = prepared.aovs_changed;
    auto &lightgroups_changed = prepared.lightgroups_changed;
    auto &crypto = prepared.crypto;
    auto &crypto_changed = prepared.crypto_changed;
    auto &preparation_started = prepared.preparation_started;
    auto &effective_world = prepared.effective_world;
    auto &world_changed = prepared.world_changed;
    auto &prepared_world = prepared.prepared_world;
    auto &effective_lights = prepared.effective_lights;
    auto &prepared_lights = prepared.prepared_lights;
    auto &prepared_graphs = prepared.prepared_graphs;
    auto &effective_materials = prepared.effective_materials;
    auto &prepared_bindings = prepared.prepared_bindings;
    auto &pose_records = prepared.pose_records;
    // Joining a second display must not cancel/restart an identical render.
    // Compare owned definitions, not mutable engine geometry. Texture/OCIO
    // preparation above and full validation remain authoritative on this path.
    const auto material_same = [&](const MaterialData &m) {
      const auto old = i.material_definitions.find(m.key);
      return old != i.material_definitions.end() && old->second == m;
    };
    const auto light_same = [&](const LightData &l) {
      const auto old = i.lights.find(l.key);
      return old != i.lights.end() && old->second.definition && *old->second.definition == l;
    };
    const auto object_same = [&](const ObjectData &o) {
      const auto old = i.objects.find(o.key);
      if (old == i.objects.end())
        return false;
      const auto &r = old->second;
      return r.geometry_key == o.geometry_key && r.matrix == o.transform &&
             r.nominal_transform == o.nominal_transform && r.motion == o.motion && r.random_id == o.random_id &&
             r.visibility == o.visibility && r.properties == o.properties && r.attributes == o.attributes;
    };
    const auto poses_same = [&]() {
      for (std::size_t n = 0; n < u.object_poses.size(); ++n) {
        const auto &p = u.object_poses[n];
        const auto &r = *pose_records[n];
        if (r.matrix != p.transform || r.nominal_transform != p.nominal_transform || r.motion != p.motion)
          return false;
      }
      return true;
    };
    const auto attributes_same = [&]() {
      for (std::size_t n = 0; n < u.object_attributes.size(); ++n)
        if (prepared.attribute_records.at(n)->attributes != u.object_attributes[n].attributes)
          return false;
      return true;
    };
    const auto attribute_poses_same = [&]() {
      for (std::size_t n = 0; n < u.object_attributes.size(); ++n)
        if (const auto &pose = u.object_attributes[n].pose) {
          const auto &record = *prepared.attribute_records.at(n);
          if (record.matrix != pose->transform || record.motion != pose->motion ||
              record.nominal_transform != pose->nominal_transform)
            return false;
        }
      return true;
    };
    const bool refine_camera = !hold_dicing && (!u.defer_camera_subdivision || force_dicing) &&
                               (dicing_projection_changed || i.suspended_camera_refinement) &&
                               std::any_of(i.meshes.begin(), i.meshes.end(),
                                           [](const auto &item) { return item.second.pixel_adaptive_subdivision(); });
    const auto background_emission = u.background.value_or(i.background_definition.value_or(Float3{}));
    bool world_surface_emission = background_emission.x > 0 || background_emission.y > 0 || background_emission.z > 0;
    if (effective_world.graph) {
      world_surface_emission = i.world_surface_emission;
      if (prepared_world) {
        // Use the engine's own conservative emission estimator on an
        // exclusively owned graph, never on a concurrently rendering node.
        ccl::Shader estimate;
        estimate.set_graph(std::move(prepared_world));
        estimate.estimate_emission();
        world_surface_emission = !ccl::is_zero(estimate.emission_estimate);
        prepared_world = std::move(estimate.graph);
      }
    }
    const auto &linking = u.linking ? *u.linking : i.linking_definition;
    const bool linking_settings_changed = linking != i.linking_definition;
    const bool linking_rebuild =
        linking_settings_changed || ((!linking.receivers.empty() || !linking.blockers.empty()) &&
                                     (!u.objects.empty() || !u.deleted_objects.empty() || u.background || u.world));
    std::optional<LinkingPlan> prepared_linking;
    if (linking_rebuild) {
      std::set<std::string> active, removed(u.deleted_objects.begin(), u.deleted_objects.end()), replaced;
      for (const auto &object : u.objects)
        replaced.insert(object.key);
      for (const auto &[key, record] : i.objects)
        if (!removed.contains(key) && !replaced.contains(key))
          active.insert(record.properties.linking_id.empty() ? key : record.properties.linking_id);
      for (const auto &object : u.objects)
        active.insert(object.properties.linking_id.empty() ? object.key : object.properties.linking_id);
      const bool world_active = world_surface_emission && (effective_world.visibility & 30u);
      prepared_linking = prepare_linking(linking, i.linking_plan, &active, world_active);
    }
    const bool linking_plan_changed = prepared_linking && *prepared_linking != i.linking_plan;
    const auto dicing_render_state = [](const DicingSettings &d) {
      return std::tie(d.offscreen_scale, d.camera, d.update, d.refresh_revision);
    };
    const bool unchanged =
        reuse_if_unchanged && !iris_changed && !linking_settings_changed && !linking_plan_changed && !refine_camera &&
        i.generation && !i.color_update_pending &&
        (i.state == SessionState::Rendering || i.state == SessionState::Paused || i.state == SessionState::Complete) &&
        !i.session->device->have_error() && !i.session->progress.get_error() && prepared_graphs.empty() &&
        prepared_bindings.empty() && prepared_lights.empty() && !prepared_world && !world_changed && u.meshes.empty() &&
        u.volumes.empty() && u.points.empty() && u.curves.empty() && u.deleted_objects.empty() &&
        u.deleted_geometry.empty() && u.deleted_materials.empty() && u.deleted_lights.empty() &&
        std::all_of(u.materials.begin(), u.materials.end(), material_same) &&
        std::all_of(u.lights.begin(), u.lights.end(), light_same) &&
        std::all_of(u.objects.begin(), u.objects.end(), object_same) && poses_same() && attributes_same() &&
        attribute_poses_same() && (!u.camera || i.camera_definition == u.camera) &&
        (!u.dicing || dicing_render_state(i.dicing_settings) == dicing_render_state(*u.dicing)) &&
        (!u.background || i.background_definition == u.background) &&
        (!u.denoising || *u.denoising == i.config.denoising) && (!u.rendering || *u.rendering == i.rendering) &&
        (!u.passes || *u.passes == i.config.passes) && !aovs_changed && !lightgroups_changed && !crypto_changed &&
        (!u.background_lightgroup || i.background_lightgroup_definition == u.background_lightgroup) &&
        (!u.background_shadow_catcher || i.background_shadow_catcher_definition == u.background_shadow_catcher) &&
        (!u.transparent_background || i.transparent_background_definition == u.transparent_background) &&
        (!u.transparent_glass || i.transparent_glass_definition == u.transparent_glass) &&
        (!u.transparent_roughness_threshold ||
         i.transparent_roughness_definition == u.transparent_roughness_threshold) &&
        (!u.motion_blur || i.motion_blur_definition == u.motion_blur) &&
        (!u.crop || (u.crop->x == i.crop.x && u.crop->y == i.crop.y && u.crop->width == i.crop.width &&
                     u.crop->height == i.crop.height));
    if (unchanged) {
      if (u.source_diagnostics)
        i.source_diagnostics = *u.source_diagnostics;
      std::lock_guard frame_lock(i.store->mutex);
      if (!i.store->interrupted && i.store->error.empty()) {
        if (u.dicing && u.dicing->topology_cache_mib != i.dicing_settings.topology_cache_mib) {
          i.set_topology_cache_limit(u.dicing->topology_cache_mib);
          i.dicing_settings.topology_cache_mib = u.dicing->topology_cache_mib;
        }
        ++i.unchanged_reuses;
        i.direct_attribute_updates = 0;
        i.expanded_attribute_updates = 0;
        i.attribute_fallback_reason.clear();
        profile.mark("reuse_check");
        profile.accept(i.generation);
        return i.generation;
      }
    }
    // Camera/object poses have valid interactive reprojection. Resource, optics,
    // sampling or output-layout changes invalidate the temporal content instead.
    const auto camera_history_state = [](const CameraData &c) {
      return std::tie(c.short_axis_fov, c.near_clip, c.far_clip, c.orthographic, c.ortho_scale, c.aperture_size,
                      c.focus_distance, c.aperture_rotation, c.aperture_ratio, c.aperture_blades, c.image_iris,
                      c.image_iris_method, c.image_iris_color, c.pixel_aspect, c.panorama, c.sampling);
    };
    const bool invalidating_objects =
        !attributes_same() || std::any_of(u.objects.begin(), u.objects.end(), [&](const auto &object) {
          const auto previous = i.objects.find(object.key);
          return previous == i.objects.end() || previous->second.geometry_key != object.geometry_key ||
                 previous->second.properties != object.properties || previous->second.attributes != object.attributes;
        });
    const bool reset_denoising_history =
        !u.meshes.empty() || !u.points.empty() || !u.curves.empty() || !u.volumes.empty() ||
        !effective_materials.empty() || !prepared_bindings.empty() || !effective_lights.empty() ||
        u.world.has_value() || !u.deleted_geometry.empty() || !u.deleted_objects.empty() ||
        !u.deleted_materials.empty() || !u.deleted_lights.empty() || invalidating_objects ||
        (u.camera &&
         (!i.camera_definition || camera_history_state(*u.camera) != camera_history_state(*i.camera_definition))) ||
        (u.denoising && *u.denoising != i.config.denoising) || (u.rendering && *u.rendering != i.rendering) ||
        (u.passes && *u.passes != i.config.passes) || aovs_changed || lightgroups_changed || crypto_changed ||
        u.background.has_value() || u.background_lightgroup.has_value() || u.background_shadow_catcher.has_value() ||
        u.transparent_background.has_value() || u.transparent_glass.has_value() ||
        u.transparent_roughness_threshold.has_value() || u.motion_blur.has_value() || u.crop.has_value() ||
        i.color_update_pending;
    const double graph_preparation = seconds(preparation_started);
    // Camera/transform/shader-value edits do not rebuild potentially large manifests.
    const bool metadata_dirty =
        crypto_changed || ((crypto.object || crypto.material || crypto.asset) && !i.store->output_metadata) ||
        !u.deleted_objects.empty() || !u.deleted_materials.empty() || !u.deleted_lights.empty() ||
        std::any_of(u.objects.begin(), u.objects.end(),
                    [&](const auto &o) {
                      const auto old = i.objects.find(o.key);
                      return old == i.objects.end() ||
                             old->second.properties.cryptomatte_object != o.properties.cryptomatte_object ||
                             old->second.properties.cryptomatte_asset != o.properties.cryptomatte_asset;
                    }) ||
        std::any_of(u.materials.begin(), u.materials.end(),
                    [&](const auto &m) {
                      const auto old = i.material_definitions.find(m.key);
                      return old == i.material_definitions.end() || old->second.cryptomatte_name != m.cryptomatte_name;
                    }) ||
        std::any_of(u.lights.begin(), u.lights.end(), [&](const auto &l) {
          const auto old = i.lights.find(l.key);
          return old == i.lights.end() || !old->second.definition ||
                 old->second.definition->cryptomatte_object != l.cryptomatte_object ||
                 old->second.definition->cryptomatte_asset != l.cryptomatte_asset;
        });
    const auto started = Clock::now();
    profile.mark("update_setup");
    const bool follows_render_camera = i.config.interactive && i.config.persistent_updates &&
                                       !prepared.desired_dicing.camera && !hold_dicing && !force_dicing;
    const bool camera_refinement =
        follows_render_camera && refine_camera && u.meshes.empty() && u.volumes.empty() && u.curves.empty() &&
        u.points.empty() && u.objects.empty() && u.object_poses.empty() && u.object_attributes.empty() &&
        u.deleted_geometry.empty() && u.deleted_objects.empty() && effective_materials.empty() &&
        prepared_bindings.empty() && !u.motion_blur && !prepared.recalculate_dicing;
    Impl::EngineUpdatePlan plan{.prepared_linking = std::move(prepared_linking),
                                .world_surface_emission = world_surface_emission,
                                .linking_plan_changed = linking_plan_changed,
                                .reset_denoising_history = reset_denoising_history,
                                .graph_preparation = graph_preparation,
                                .metadata_dirty = metadata_dirty,
                                .automatic_camera_refinement = camera_refinement,
                                .started = started};
    const auto transaction = [&]() { i.mutate_engine(u, prepared, plan, profile); };
    mutation_started = true;
    if (follows_render_camera && i.automatic_camera_refinement && u.camera && dicing_projection_changed &&
        i.state != SessionState::Cancelled && !i.session->progress.get_error() && i.pending_camera_tessellation()) {
      // Scene synchronization interrupts path tracing. An adaptive geometry
      // update needs the normal cancellation join before we can replace it.
      i.cancel_engine();
      i.suspended_camera_refinement = true;
      plan.cancelled_camera_refinement = true;
    }
    i.run_engine_transaction(transaction);
    i.last_update_seconds = seconds(started);
    profile.mark("finish");
    profile.accept(i.generation);
    return i.generation;
  } catch (const std::invalid_argument &error) {
    if (!mutation_started)
      throw SceneUpdateRejected(error.what());
    throw;
  }
}
void RenderSession::prepare_color_configuration_change() {
  std::lock_guard lock(impl_->control);
  impl_->ensure_open();
  if (!impl_->color_update_pending)
    impl_->invalidate_engine_color(impl_->target_color_epoch + 1);
}
void RenderSession::pause(bool paused) {
  std::lock_guard lock(impl_->control);
  impl_->ensure_open();
  impl_->paused = paused;
  if (!impl_->color_update_pending)
    impl_->session->set_pause(paused);
  impl_->state =
      paused ? SessionState::Paused : (impl_->color_update_pending ? SessionState::Idle : SessionState::Rendering);
}
void RenderSession::cancel() {
  std::lock_guard lock(impl_->control);
  impl_->ensure_open();
  impl_->cancel_engine();
  // Pause belongs to the stopped render. A subsequent explicit render starts
  // tracing, while ordinary scene edits during Pause still remain paused.
  impl_->paused = false;
  {
    std::lock_guard frame_lock(impl_->store->mutex);
    if (impl_->generation && impl_->store->finished_seconds == 0)
      impl_->store->finished_seconds = seconds(impl_->store->started);
  }
  impl_->state = SessionState::Cancelled;
}
void RenderSession::close() {
  if (!impl_)
    return;
  std::lock_guard lock(impl_->control);
  if (impl_->session) {
    impl_->cancel_engine();
    {
      std::lock_guard frame_lock(impl_->store->mutex);
      impl_->store->focus.reset();
      if (impl_->generation && impl_->store->finished_seconds == 0)
        impl_->store->finished_seconds = seconds(impl_->store->started);
    }
    impl_->session.reset();
    impl_->tile_storage.reset();
    impl_->materials.clear();
    impl_->retired_shaders.clear();
    impl_->deferred_retired_shaders.clear();
    impl_->source_diagnostics.clear();
    impl_->displacement_motion_diagnostics.clear();
    impl_->material_definitions.clear();
    impl_->material_requirements.clear();
    impl_->binding_materials.clear();
    impl_->recovered_requirements.clear();
    impl_->volume_recovery.clear();
    impl_->texture_watch_paths.clear();
    impl_->texture_watch_revisions.clear();
    impl_->texture_watch_auto.clear();
    impl_->light_watch_paths.clear();
    impl_->light_watch_revisions.clear();
    impl_->light_watch_auto.clear();
    impl_->light_requirements.clear();
    impl_->world_definition.reset();
    impl_->world_requirements = {};
    impl_->world_watch_paths.clear();
    impl_->world_watch_revision.clear();
    impl_->world_watch_auto = false;
    impl_->linking_definition = {};
    impl_->linking_plan = {};
    impl_->image_iris.reset();
    impl_->image_iris_watch_path.clear();
    impl_->image_iris_watch_revision.clear();
    impl_->meshes.clear();
    impl_->objects.clear();
    impl_->object_attribute_bytes = 0;
    impl_->lights.clear();
    impl_->pending_geometry_retirement.clear();
  }
  impl_->state = SessionState::Closed;
  release_color(this);
}
std::map<std::string, std::shared_ptr<const MeshData>> RenderSession::retained_mesh_sources() const {
  std::lock_guard lock(impl_->control);
  std::map<std::string, std::shared_ptr<const MeshData>> result;
  for (const auto &[key, record] : impl_->meshes)
    if (record.source) {
      if (record.source->key != key || record.source->revision != record.revision)
        throw std::logic_error("Retained mesh source lost its resource ownership");
      result.emplace(key, record.source);
    }
  return result;
}
SessionStatus RenderSession::status() const {
  std::lock_guard lock(impl_->control);
  const auto &i = *impl_;
  SessionStatus s;
  s.state = i.state;
  s.generation = i.generation;
  s.geometry_conversions = i.geometry_conversions;
  s.object_updates = i.object_updates;
  s.direct_attribute_updates = i.direct_attribute_updates;
  s.expanded_attribute_updates = i.expanded_attribute_updates;
  s.attribute_fallback_reason = i.attribute_fallback_reason;
  s.attribute_fallback_count = i.attribute_fallback_count;
  s.last_update_seconds = i.last_update_seconds;
  s.unchanged_reuses = i.unchanged_reuses;
  s.image_iris_preparations = i.image_iris_preparations;
  if (i.image_iris) {
    s.image_iris_table_bytes = i.image_iris->table.size() * sizeof(float);
    s.image_iris_preparation_seconds = i.image_iris->preparation_seconds;
  }
  s.denoiser = i.denoise_policy.backend;
  s.denoise_device_id = i.denoise_policy.device_id;
  s.render_device_ids = i.render_device_ids;
  s.source_diagnostics = i.source_diagnostics;
  if (!i.denoise_policy.warning.empty())
    s.source_diagnostics.push_back({"render.dlss_motion",
                                    {},
                                    {},
                                    i.denoise_policy.warning,
                                    "Disable the Motion Vectors pass and shutter motion blur to use DLSS in IPR."});
  if (i.camera_definition)
    s.source_diagnostics.insert(s.source_diagnostics.end(), i.camera_definition->source_diagnostics.begin(),
                                i.camera_definition->source_diagnostics.end());
  if (i.dicing_settings.camera)
    for (auto issue : i.dicing_settings.camera->source_diagnostics) {
      issue.code = "dicing_" + issue.code;
      issue.message = "Dicing Camera: " + issue.message;
      s.source_diagnostics.push_back(std::move(issue));
    }
  s.source_diagnostics.insert(s.source_diagnostics.end(), i.linking_plan.diagnostics.begin(),
                              i.linking_plan.diagnostics.end());
  std::erase_if(s.source_diagnostics, [&](const auto &issue) {
    const bool uv = issue.code == "geometry.shutter_uv", color = issue.code == "geometry.shutter_color",
               named = issue.code == "geometry.shutter_attribute";
    if (!uv && !color && !named)
      return false;
    if (!i.motion_blur_definition.value_or(false))
      return true;
    const auto found = i.meshes.find(issue.resource);
    if (found == i.meshes.end())
      return false;
    const auto &record = found->second;
    if (record.source && !record.source->motion_blur)
      return true;
    if (uv && std::find(i.config.passes.begin(), i.config.passes.end(), "uv") != i.config.passes.end())
      return false;
    for (const auto &material : record.materials) {
      const auto &definition = i.material_definitions.at(material);
      if (color && definition.color_attribute == "Cd" &&
          (definition.kind == MaterialKind::Diffuse || definition.kind == MaterialKind::Principled ||
           definition.kind == MaterialKind::Emission))
        return false;
      if (auto r = i.material_requirements.find(material); r != i.material_requirements.end()) {
        if (named) {
          if (r->second.named_attributes.contains(issue.attribute))
            return false;
          continue;
        }
        if (uv ? (r->second.requires_uv || r->second.displacement_requires_uv)
               : (r->second.requires_color || r->second.displacement_requires_color))
          return false;
      }
    }
    return true;
  });
  s.source_diagnostics.insert(s.source_diagnostics.end(), i.displacement_motion_diagnostics.begin(),
                              i.displacement_motion_diagnostics.end());
  for (const auto &[key, issues] : i.volume_recovery)
    s.source_diagnostics.insert(s.source_diagnostics.end(), issues.begin(), issues.end());
  // Attribute capture diagnostics are relevant only to active shader inputs
  // (or the UV output pass). Do not warn about unused asset attributes.
  const auto attribute_problem = [](const SourceDiagnostic &issue) {
    return issue.code == "geometry.invalid_uv" || issue.code == "geometry.invalid_color" ||
           issue.code == "geometry.invalid_attribute";
  };
  std::erase_if(s.source_diagnostics, [&](const auto &issue) {
    return attribute_problem(issue) &&
           !(issue.code == "geometry.invalid_uv" &&
             std::find(i.config.passes.begin(), i.config.passes.end(), "uv") != i.config.passes.end());
  });
  // Index capture diagnostics once. Matching each recovered binding against
  // the whole list made every status() O(meshes x diagnostics); status runs
  // on every worker reply. The last matching record wins, as before.
  std::map<std::pair<std::string, std::string>, const SourceDiagnostic *> captured_by_code;
  std::map<std::tuple<std::string, std::string, std::string>, const SourceDiagnostic *> captured_by_attribute;
  for (const auto &original : i.source_diagnostics)
    if (original.code == "geometry.invalid_uv" || original.code == "geometry.invalid_color" ||
        original.code == "geometry.invalid_attribute") {
      captured_by_code[{original.resource, original.code}] = &original;
      captured_by_attribute[{original.resource, original.code, original.attribute}] = &original;
    }
  for (const auto &[key, record] : i.meshes)
    if (record.kind != Impl::GeometryKind::Volume) {
      std::optional<bool> shown;
      for (const auto &material : record.materials)
        if (auto found = i.recovered_requirements.find(binding_key(material, record.has_uv, record.has_color,
                                                                   record.kind != Impl::GeometryKind::Mesh,
                                                                   record.attributes, record.instance_attributes));
            found != i.recovered_requirements.end()) {
          if (found->second.source_diagnostics.empty())
            continue;
          // A geometry no object shows has nothing to shade and no instance attributes to read:
          // a packed prototype whose instances are all collapsed to zero scale (particles on
          // their birth frame) must not report the attributes its instances would supply.
          // Looked up only for a geometry that has something to report.
          if (!shown)
            shown = std::any_of(i.objects.begin(), i.objects.end(),
                                [&](const auto &object) { return object.second.geometry_key == key; });
          if (!*shown)
            continue;
          const auto &definition = i.material_definitions.at(material);
          std::set<std::string> reported;
          for (auto issue : found->second.source_diagnostics) {
            // Link renders are Blender-exact: Blender says nothing when a medium
            // does not read a mesh attribute, so neither does a Link mesh.
            if (issue.code == "material.mesh_volume_attribute" && key.starts_with("blend/"))
              continue;
            const bool per_attribute = issue.code == "material.mesh_volume_attribute";
            if (!reported.insert(per_attribute ? issue.code + '\0' + issue.attribute : issue.code).second)
              continue;
            const auto code = issue.code == "material.missing_uv"          ? "geometry.invalid_uv"
                              : issue.code == "material.missing_color"     ? "geometry.invalid_color"
                              : issue.code == "material.missing_attribute" ? "geometry.invalid_attribute"
                                                                           : "";
            const SourceDiagnostic *original = nullptr;
            if (issue.code == "material.missing_attribute") {
              if (const auto match = captured_by_attribute.find({key, code, issue.attribute});
                  match != captured_by_attribute.end())
                original = match->second;
            } else if (const auto match = captured_by_code.find({key, code}); match != captured_by_code.end())
              original = match->second;
            if (original)
              issue.message = original->source + ": " + original->message;
            issue.source = definition.cryptomatte_name.empty() ? material : definition.cryptomatte_name;
            issue.resource = key;
            s.source_diagnostics.push_back(std::move(issue));
          }
        }
    }
  std::set<std::string> reported_uv_resources;
  for (const auto &issue : s.source_diagnostics)
    if (issue.code == "material.missing_uv")
      reported_uv_resources.insert(issue.resource);
  std::erase_if(s.source_diagnostics, [&](const auto &issue) {
    return issue.code == "geometry.invalid_uv" && reported_uv_resources.contains(issue.resource);
  });
  for (auto issue : i.world_requirements.source_diagnostics) {
    if (i.world_definition && !i.world_definition->source_path.empty())
      issue.source = i.world_definition->source_path;
    else if (issue.code == "world.volume_source_unavailable")
      issue.source = "World Volume";
    s.source_diagnostics.push_back(std::move(issue));
  }
  std::set<std::string> light_issues;
  for (const auto &[key, required] : i.light_requirements)
    for (auto issue : required.source_diagnostics) {
      const auto &definition = *i.lights.at(key).definition;
      issue.resource = key;
      issue.source = definition.source_path.empty() ? key : definition.source_path;
      if (!light_issues.insert(issue.source + '\0' + issue.code + '\0' + issue.message).second)
        continue;
      s.source_diagnostics.push_back(std::move(issue));
    }
  for (const auto &[key, material] : i.material_definitions)
    s.source_diagnostics.insert(s.source_diagnostics.end(), material.source_diagnostics.begin(),
                                material.source_diagnostics.end());
  for (const auto &[key, requirements] : i.material_requirements)
    if (!temporal_material(key) || i.temporal_diagnostic_materials.contains(key))
      for (auto issue : requirements.source_diagnostics) {
        issue.resource = key;
        if (issue.code == "material.unsupported_attribute" || issue.code == "material.missing_attribute" ||
            issue.code == "material.volume_source_unavailable" || issue.code == "material.shader_domain" ||
            issue.code == "material.displacement_scene_query" || issue.code == "material.ramp_unavailable" ||
            issue.code == "material.curve_unavailable") {
          const auto &material = i.material_definitions.at(key);
          if (!material.cryptomatte_name.empty())
            issue.source = material.cryptomatte_name;
          else if (issue.code != "material.displacement_scene_query")
            issue.source = key;
        }
        s.source_diagnostics.push_back(std::move(issue));
      }
  s.rendering = i.rendering;
  if (i.session)
    s.current_sample = i.session->progress.get_current_sample();
  s.color_update_pending = i.color_update_pending;
  s.color_epoch = i.source_color_epoch;
  s.color_target_epoch = i.target_color_epoch;
  s.geometry_resources = i.meshes.size();
  s.object_resources = i.objects.size();
  s.material_resources = std::count_if(i.materials.begin(), i.materials.end(),
                                       [](const auto &value) { return !temporal_material(value.first); });
  s.light_resources = i.lights.size();
  s.engine_shader_resources = i.session ? i.session->scene->shaders.size() : 0;
  s.retired_shader_resources = i.retired_shaders.size() + i.deferred_retired_shaders.size();
  s.shader_reuses = i.shader_reuses;
  s.displacement_rebuilds = i.displacement_rebuilds;
  s.mesh_refits = i.mesh_refits;
  s.volume_vector_merges = i.volume_vector_merges;
  s.subdivision_tessellations = i.subdivision_tessellations.load();
  if (i.session) {
    const auto cache = i.session->scene->geometry_manager->subdivision_osd_cache_stats();
    s.subdivision_topology_cache_hits = cache.hits;
    s.subdivision_topology_cache_misses = cache.misses;
    s.subdivision_topology_cache_admissions = cache.admissions;
    s.subdivision_topology_cache_evictions = cache.evictions;
    s.subdivision_topology_cache_oversized = cache.oversized;
    s.subdivision_topology_cache_entries = cache.entries;
    s.subdivision_topology_cache_bytes = cache.retained_bytes;
    s.subdivision_topology_cache_budget_bytes = cache.budget_bytes;
    s.subdivision_topology_cache_active_retired_bytes = cache.active_retired_bytes;
  }
  std::set<ccl::Geometry *> counted_geometry;
  std::set<const MeshData *> counted_sources;
  std::unordered_set<const Impl::MeshFamilyMembers *> counted_families;
  std::unordered_set<const Impl::MeshRenderState *> counted_render_states;
  std::unordered_set<const void *> counted_source_arrays;
  for (const auto &[key, m] : i.meshes) {
    s.volume_vector_fields += m.volume_vectors.size();
    for (const auto &[name, vector] : m.volume_vectors)
      s.volume_vector_source_bytes += vector.bytes;
    std::size_t bytes = 0, physical_count = 0;
    if (m.sharing_family && counted_families.insert(m.sharing_family.get()).second) {
      ++s.mesh_family_resources;
      s.mesh_family_members += m.sharing_family->members.size();
    }
    // Count actual engine meshes once, independent of logical bindings.
    if (m.source) {
      if (!m.render)
        throw std::logic_error("Owned mesh has no physical render state");
      if (counted_render_states.insert(m.render.get()).second) {
        s.mesh_variant_bindings += 1 + m.render->variants.size();
        s.mesh_metric_records += m.render->subdivision_metrics.size();
        if (!m.render->subdivision_metrics.empty()) {
          for (const auto &[node, metric] : m.render->subdivision_metrics) {
            if (!counted_geometry.insert(node).second)
              continue;
            ++physical_count;
            std::lock_guard lock(metric->mutex);
            bytes += metric->bytes;
            if (m.source->subdivision && metric->ready) {
              s.subdivision_vertices += metric->vertices;
              s.subdivision_triangles += metric->triangles;
            }
          }
          if (physical_count && m.source->subdivision)
            s.subdivision_control_faces += m.source->subdivision->face_sizes.size();
        } else {
          if (counted_geometry.insert(m.node).second) {
            bytes = m.bytes;
            physical_count = 1;
          }
          for (const auto &[variant, node] : m.render->variants)
            if (counted_geometry.insert(node).second) {
              bytes += m.bytes;
              ++physical_count;
            }
        }
      }
    } else if (counted_geometry.insert(m.node).second) {
      bytes = m.bytes;
    }
    if (m.source) {
      std::uint64_t retained =
          m.seam_vertices.size() * sizeof(int) + (m.island_random.size() + m.pointiness.size()) * sizeof(float);
      for (const auto *source : {m.source.get(), m.seam_source.get(), m.nominal_source.get(), m.sharing_source.get()})
        if (source && counted_sources.insert(source).second)
          retained += mesh_owned_heap_bytes(*source, counted_source_arrays);
      s.mesh_variant_resources += physical_count;
      s.mesh_variant_bytes += bytes;
      s.retained_mesh_source_bytes += retained;
      s.owned_geometry_bytes += bytes + retained;
    } else
      s.owned_geometry_bytes += bytes + m.source_bytes + m.seam_source_bytes + m.nominal_source_bytes;
  }
  // Polling must not walk every instance and shutter field merely to report
  // memory. The exact sum changes only when owned attributes change or retire.
  s.owned_geometry_bytes += i.object_attribute_bytes;
  {
    std::lock_guard frame_lock(i.store->mutex);
    s.error = i.store->error;
    s.first_image_seconds = i.store->first_image_seconds;
    s.elapsed_seconds =
        i.store->finished_seconds > 0 ? i.store->finished_seconds : (i.generation ? seconds(i.store->started) : 0);
    if (i.store->complete && s.state == SessionState::Rendering)
      s.state = SessionState::Complete;
    if (i.store->latest && i.store->latest->generation == i.generation)
      s.current_sample = std::max(s.current_sample, i.store->latest->samples);
  }
  if (i.session) {
    s.volume_ray_marching = i.session->scene->integrator->get_volume_ray_marching();
    s.engine_nested_dielectrics = i.session->scene->integrator->get_nested_dielectrics();
#ifdef CYCLES_HOUDINI_HAS_NESTED_WARNINGS
    if (s.engine_nested_dielectrics) {
      const auto *scene = i.session->scene.get();
      s.nested_probe_unresolved = scene->nested_probe_unresolved_count();
      s.nested_stack_depth_clamped = scene->nested_stack_depth_was_clamped();
      s.nested_stack_depth = scene->nested_stack_depth_value();
      if (s.nested_probe_unresolved)
        s.source_diagnostics.push_back(
            {"render.nested_probe_limit", "Nested Dielectrics", "",
             "Nested Dielectrics: medium could not be determined for " + std::to_string(s.nested_probe_unresolved) +
                 " camera rays (probe limit). Check for very dense geometry above the camera.",
             "Those rays start in the wrong medium. Reduce the geometry stacked above the camera or the number of "
             "participating objects around it.",
             "warning", ""});
      if (s.nested_stack_depth_clamped)
        s.source_diagnostics.push_back({"render.nested_depth_limit", "Nested Dielectrics", "",
                                        "Nested Dielectrics: more participating objects overlap than can be tracked (" +
                                            std::to_string(s.nested_stack_depth) +
                                            " media); the lowest-priority ones are ignored at those points.",
                                        "Reduce how many participating objects overlap in one place.", "warning", ""});
    }
#endif
    if (i.config.collect_diagnostics) {
      bool first = true;
      for (const auto &[key, record] : i.objects)
        if (record.node) {
          const int priority = record.node->get_dielectric_priority();
          if (priority != 0)
            ++s.dielectric_priority_objects;
          if (first || priority < s.dielectric_priority_min)
            s.dielectric_priority_min = priority;
          if (first || priority > s.dielectric_priority_max)
            s.dielectric_priority_max = priority;
          first = false;
        }
    }
    std::string substatus;
    i.session->progress.get_status(s.message, substatus);
    if (!substatus.empty())
      s.message += " / " + substatus;
    s.progress = i.session->progress.get_progress();
    // Cycles Stats counts allocated device buffers, including host-backed CPU/fallback memory.
    // This deliberately does not claim to measure physical GPU residency.
    s.device_allocation_bytes = atomic_add_and_fetch_z(&i.session->stats.mem_used, 0);
    s.device_allocation_peak_bytes = atomic_add_and_fetch_z(&i.session->stats.mem_peak, 0);
    if (i.session->device->have_error())
      s.error = i.session->device->error_message();
    else if (i.session->progress.get_error())
      s.error = i.session->progress.get_error_message();
  }
  if (!s.error.empty())
    s.state = SessionState::Failed;
  // Adaptive convergence and time limits can finish before the maximum
  // sample budget. Successful completion is still 100% of the render work.
  if (s.state == SessionState::Complete)
    s.progress = 1.0;
  return s;
}
bool RenderSession::textures_changed() const {
  std::lock_guard lock(impl_->control);
  impl_->ensure_open();
  if (impl_->color_update_pending)
    return true;
  if (!impl_->image_iris_watch_path.empty() &&
      image_iris_revision(impl_->image_iris_watch_path) != impl_->image_iris_watch_revision)
    return true;
  const auto input_policy =
      impl_->config.texture_input_policy.identity ? impl_->config.texture_input_policy.identity() : std::string{};
  if (!impl_->world_watch_paths.empty() &&
      texture_watch_revision(impl_->world_watch_paths, impl_->world_watch_auto ? input_policy : std::string{}) !=
          impl_->world_watch_revision)
    return true;
  for (const auto &[key, paths] : impl_->texture_watch_paths)
    if (texture_watch_revision(paths, impl_->texture_watch_auto.at(key) ? input_policy : std::string{}) !=
        impl_->texture_watch_revisions.at(key))
      return true;
  for (const auto &[key, paths] : impl_->light_watch_paths)
    if (texture_watch_revision(paths, impl_->light_watch_auto.at(key) ? input_policy : std::string{}) !=
        impl_->light_watch_revisions.at(key))
      return true;
  return false;
}
std::vector<TextureInputInfo> RenderSession::texture_inputs() const {
  std::lock_guard lock(impl_->control);
  impl_->ensure_open();
  std::vector<TextureInputInfo> result;
  for (const auto &[key, requirements] : impl_->material_requirements)
    for (const auto &asset : requirements.textures)
      result.push_back(
          {key, asset.filename, asset.display_colorspace, asset.selection, asset.input_policy, asset.revision,
           asset.tiles, asset.input_processor ? asset.decoded_pixels * 16 : 0,
           asset.input_processor && !asset.memory_image ? "float32 RGBA OpenEXR, 64x64 tiles, mipmaps" : ""});
  for (const auto &asset : impl_->world_requirements.textures)
    result.push_back(
        {"World", asset.filename, asset.display_colorspace, asset.selection, asset.input_policy, asset.revision,
         asset.tiles, asset.input_processor ? asset.decoded_pixels * 16 : 0,
         asset.input_processor && !asset.memory_image ? "float32 RGBA OpenEXR, 64x64 tiles, mipmaps" : ""});
  for (const auto &[key, required] : impl_->light_requirements)
    for (const auto &asset : required.textures)
      result.push_back(
          {key, asset.filename, asset.display_colorspace, asset.selection, asset.input_policy, asset.revision,
           asset.tiles, asset.input_processor ? asset.decoded_pixels * 16 : 0,
           asset.input_processor && !asset.memory_image ? "float32 RGBA OpenEXR, 64x64 tiles, mipmaps" : ""});
  return result;
}
std::vector<PassDescriptor> RenderSession::passes() const {
  std::lock_guard lock(impl_->control);
  return impl_->store->passes;
}
SessionDiagnostics RenderSession::diagnostics() const {
  std::lock_guard lock(impl_->control);
  const auto &i = *impl_;
  SessionDiagnostics result;
  {
    std::lock_guard frame_lock(i.store->mutex);
    result = i.store->diagnostics;
  }
  result.core_update_seconds = i.last_update_seconds;
  result.cancel_join_seconds = i.cancel_join_seconds;
  result.volume_vector_assembly_seconds = i.volume_vector_assembly_seconds;
  result.scene_boundary_wait_seconds = i.scene_boundary_wait_seconds;
  result.session_cancel_calls = i.session_cancel_calls;
  result.shader_graph_preparation_seconds = i.shader_graph_preparation_seconds;
  result.cancel_output_deliveries = i.cancel_output_deliveries;
  result.cancel_display_updates = i.cancel_display_updates;
  result.cancel_output_delivery_seconds = i.cancel_output_delivery_seconds;
  result.cancel_display_conversion_seconds = i.cancel_display_conversion_seconds;
  result.cancel_output_reads_skipped = i.cancel_output_reads_skipped;
  result.cancel_display_updates_skipped = i.cancel_display_updates_skipped;
  result.material_builds = i.material_builds;
  result.light_updates = i.light_updates;
  result.background_builds = i.background_builds;
  if (!result.enabled || !i.session)
    return result;
  const auto &cancel = i.session->get_cancel_statistics();
  result.engine_update_seconds["cancel/device"] = cancel.device_seconds;
  result.engine_update_seconds["cancel/path_trace"] = cancel.path_trace_seconds;
  result.engine_update_seconds["cancel/progress"] = cancel.progress_seconds;
  result.engine_update_seconds["cancel/session_wait"] = cancel.session_wait_seconds;
  result.engine_update_seconds["cancel/total"] = cancel.total_seconds;
  auto *scene = i.session->scene.get();
  {
    // The engine publishes this record under its own short lock when an update ends; it never
    // waits for a running update and is never torn.
    ccl::DeviceUpdateMemoryRecord record;
    if (scene->get_device_update_memory(record)) {
      auto &m = result.device_update_memory;
      m.available = true;
      m.update_serial = record.update_serial;
      m.generation = record.update_tag;
      m.outcome = record.outcome;
      m.scope = "scene_device_update";
      ccl::vector<ccl::Device *> leaves;
      i.session->device->leaf_devices(leaves);
      for (const auto &entry : record.devices) {
        DeviceUpdateMemoryDevice d;
        d.id = entry.id;
        for (ccl::Device *leaf : leaves)
          if (leaf->info.id == entry.id)
            d.physical_id = physical_device_id(leaf->info);
        d.backend = entry.backend;
        d.description = entry.description;
        d.start_bytes = entry.start_bytes;
        d.peak_bytes = entry.peak_bytes;
        d.end_bytes = entry.end_bytes;
        d.max_build_temp_bytes = entry.max_build_temp_bytes;
        d.max_build_temp_allocation_bytes = entry.max_build_temp_allocation_bytes;
        d.build_count = entry.build_count;
        d.reused_temp_build_count = entry.reused_temp_build_count;
        d.local_temp_build_count = entry.local_temp_build_count;
        d.kept_temp_bytes_before_attributes = entry.kept_temp_bytes_before_attributes;
        m.devices.push_back(std::move(d));
      }
    }
  }
  const auto snapshot_started = Clock::now();
  ccl::thread_scoped_lock scene_lock(scene->mutex, std::try_to_lock);
  result.engine_snapshot_wait_seconds = seconds(snapshot_started);
  if (!scene_lock.owns_lock()) {
    result.engine_statistics_pending = true;
    return result;
  }
  if (scene->update_stats) {
    const auto &stats = *scene->update_stats;
    const auto add = [&](const char *group, const ccl::UpdateTimeStats &times) {
      for (const auto &entry : times.times.entries) {
        // Upstream's scene/device_update timer ends at the end of the
        // shader/kernel retry loop, before the remaining managers.
        const auto name = std::string(group) == "scene" && entry.name == "device_update"
                              ? "shader_prepass_and_kernel_load"
                              : entry.name;
        result.engine_update_seconds[std::string(group) + "/" + name] += entry.time;
      }
    };
    add("geometry", stats.geometry);
    add("image", stats.image);
    add("light", stats.light);
    add("object", stats.object);
    add("background", stats.background);
    add("bake", stats.bake);
    add("camera", stats.camera);
    add("film", stats.film);
    add("integrator", stats.integrator);
    add("osl", stats.osl);
    add("particles", stats.particles);
    add("scene", stats.scene);
    add("svm", stats.svm);
    add("tables", stats.tables);
    add("procedurals", stats.procedurals);
  }
  return result;
}
std::shared_ptr<const FrameEvent> RenderSession::frame(std::uint64_t after_sequence) const {
  std::lock_guard lock(impl_->store->mutex);
  auto result = impl_->store->latest;
  return result && result->sequence > after_sequence ? result : nullptr;
}
void RenderSession::set_preview_pass(const std::string &pass) { set_preview({pass, false}); }
std::shared_ptr<const FrameEvent> RenderSession::focus_frame(std::uint64_t after_sequence) const {
  std::lock_guard lock(impl_->store->mutex);
  const auto &store = *impl_->store;
  if (!store.focus_enabled)
    return nullptr;
  // Short renders can finish before the viewer preference arrives. The
  // completed output already owns both passes; never restart just to pick.
  auto focus = store.focus;
  if (store.latest && store.latest->generation == store.generation && store.latest->complete &&
      std::any_of(store.latest->planes.begin(), store.latest->planes.end(),
                  [](const auto &p) { return p.pass == "position"; }) &&
      std::any_of(store.latest->planes.begin(), store.latest->planes.end(),
                  [](const auto &p) { return p.pass == "depth"; }))
    focus = store.latest;
  return focus && focus->generation == store.generation && focus->sequence > after_sequence ? focus : nullptr;
}
void RenderSession::set_preview(const PreviewRequest &request) {
  const auto &pass = request.pass;
  if (pass.empty() || pass.size() > 256 || pass.find('\0') != std::string::npos)
    throw std::invalid_argument("Invalid preview pass");
  std::lock_guard lock(impl_->store->mutex);
  if (pass != "*" && std::none_of(impl_->store->passes.begin(), impl_->store->passes.end(),
                                  [&](const auto &p) { return p.id == pass; }))
    throw SceneUpdateRejected("Preview pass is not enabled: " + pass);
  if (impl_->store->preview_pass != pass || impl_->store->focus_enabled != request.focus) {
    impl_->store->preview_pass = pass;
    impl_->store->focus_enabled = request.focus;
    impl_->store->last_preview_read = {};
    if (!request.focus)
      impl_->store->focus.reset();
  }
}
} // namespace cycles_houdini
