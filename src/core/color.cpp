// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/session.h"
#include "color_internal.h"
#include "util/colorspace.h"
#include "kernel/svm/math_util.h"
#include "util/transform.h"
#include "util/md5.h"
#include "util/tbb.h"
#include <OpenColorIO/OpenColorIO.h>
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <mutex>
#include <sstream>
#include <filesystem>
#include <bit>
#include <chrono>

namespace OCIO = OCIO_NAMESPACE;
namespace cycles_houdini {
Float3 blackbody_temperature_color(float kelvin) {
  if (!std::isfinite(kelvin) || kelvin < 800 || kelvin > 20000)
    throw std::invalid_argument("Light Temperature must be finite and between 800 and 20000 K");
  auto rgb = ccl::svm_math_blackbody_color_rec709(kelvin);
  // Authoring runs in Houdini while rendering runs in another process. Use
  // its frozen authoring policy, never a previous in-process engine scene.
  const auto snapshot = current_color_configuration();
  if (snapshot->policy().output_interop_id != "lin_rec709_scene") {
    const auto config = owned_color_configuration(*snapshot);
    auto xyz_to_rgb = ccl::ColorSpaceManager::get_xyz_to_rec709();
    const auto to_scene = [&](const char *space) {
      auto transform = ccl::transform_identity();
      const auto processor = config->getProcessor(OCIO::ROLE_SCENE_LINEAR, space)->getDefaultCPUProcessor();
      processor->applyRGB(&transform.x.x);
      processor->applyRGB(&transform.y.x);
      processor->applyRGB(&transform.z.x);
      return ccl::transform_transposed_inverse(transform);
    };
    // Match ShaderManager::init_xyz_transforms, including its matrix
    // precision and exact identity shortcut for standard linear Rec.709.
    if (config->hasRole("aces_interchange")) {
      const auto aces_to_xyz = ccl::make_transform(.938280f, -.004451f, .016628f, 0.f, .337369f, .729522f, -.066890f,
                                                   0.f, .001174f, -.003711f, 1.091595f, 0.f);
      xyz_to_rgb = to_scene("aces_interchange") * ccl::transform_inverse(aces_to_xyz);
    } else if (config->hasRole("XYZ"))
      xyz_to_rgb = to_scene("XYZ");
    const auto matrix = xyz_to_rgb * ccl::transform_inverse(ccl::ColorSpaceManager::get_xyz_to_rec709());
    rgb = ccl::transform_direction(&matrix, rgb);
  }
  return {std::max(0.f, rgb.x), std::max(0.f, rgb.y), std::max(0.f, rgb.z)};
}
struct ColorConfiguration::Impl {
  OCIO::ConstConfigRcPtr config;
  std::string source_path;
  ColorPolicy policy;
  PortableColorConfiguration portable;
  OCIO::ConstCPUProcessorRcPtr to_interchange, from_interchange;
};
ColorConfiguration::ColorConfiguration(std::shared_ptr<const Impl> value) : impl_(std::move(value)) {}
ColorConfiguration::~ColorConfiguration() = default;
const ColorPolicy &ColorConfiguration::policy() const { return impl_->policy; }
class ColorConfigurationAccess {
public:
  static std::shared_ptr<const ColorConfiguration> capture(const OCIO::ConstConfigRcPtr &);
  static const OCIO::ConstConfigRcPtr &config(const ColorConfiguration &value) { return value.impl_->config; }
  static const std::string &path(const ColorConfiguration &value) { return value.impl_->source_path; }
  static const PortableColorConfiguration &portable(const ColorConfiguration &value) { return value.impl_->portable; }
  static const OCIO::ConstCPUProcessorRcPtr &interchange(const ColorConfiguration &value) {
    return value.impl_->to_interchange;
  }
  static const OCIO::ConstCPUProcessorRcPtr &from_interchange(const ColorConfiguration &value) {
    return value.impl_->from_interchange;
  }
};
namespace {
std::mutex snapshot_mutex, active_mutex;
OCIO::ConstConfigRcPtr observed_config;
std::string observed_identity;
std::shared_ptr<const ColorConfiguration> observed_snapshot, synchronized_snapshot;
std::function<std::string()> configuration_path_resolver;

tbb::task_arena &display_arena() {
  static tbb::task_arena arena(4);
  return arena;
}
template <class Function> void display_rows(int width, int height, Function apply_rows) {
  if (std::size_t(width) * height < 65536) {
    apply_rows(0, height);
    return;
  }
  // Share the same bounded arena across all conversion stages and viewers.
  display_arena().execute([&] {
    ccl::thread_capture_fp_settings();
    tbb::parallel_for(tbb::blocked_range<int>(0, height, 32),
                      [&](const auto &rows) { apply_rows(rows.begin(), rows.end()); });
  });
}

void apply_display_processor(const OCIO::ConstCPUProcessorRcPtr &processor, std::vector<float> &pixels, int width,
                             int height) {
  const auto apply_rows = [&](int begin, int end) {
    // Only RGB is exposed: OCIO must not transform renderer coverage.
    OCIO::PackedImageDesc image(pixels.data() + std::size_t(begin) * width * 4, width, end - begin, 3,
                                OCIO::BIT_DEPTH_F32, sizeof(float), 4 * sizeof(float),
                                std::ptrdiff_t(width) * 4 * sizeof(float));
    processor->apply(image);
  };
  // The processor is immutable and row ranges own disjoint RGB pixels.
  display_rows(width, height, apply_rows);
}
std::map<std::string, std::string> input_file_revisions;
std::string file_revision(const std::string &name) {
  const auto path = std::filesystem::u8path(name);
  std::error_code error;
  const auto size = std::filesystem::file_size(path, error);
  if (error)
    return "unavailable";
  const auto time = std::filesystem::last_write_time(path, error);
  if (error)
    return "unavailable";
  return std::to_string(size) + ":" + std::to_string(time.time_since_epoch().count());
}
bool input_files_changed() {
  return std::any_of(input_file_revisions.begin(), input_file_revisions.end(),
                     [](const auto &entry) { return file_revision(entry.first) != entry.second; });
}
ColorPolicy describe(const OCIO::ConstConfigRcPtr &config) {
  ColorPolicy policy;
  policy.configuration_id = config->getCacheID();
  for (int i = 0; i < config->getNumColorSpaces(); ++i)
    policy.texture_spaces.emplace_back(config->getColorSpaceNameByIndex(i));
  for (const auto &name : policy.texture_spaces)
    if (!config->getColorSpace(name.c_str())->isData())
      policy.output_spaces.push_back(name);
  const auto working = config->getColorSpace(OCIO::ROLE_SCENE_LINEAR);
  // With OCIO disabled, Cycles itself defaults to linear Rec.709.
  if (!working) {
    policy.working_space = "Linear Rec.709";
    return policy;
  }
  policy.working_space = working->getName();
  policy.default_display = config->getDefaultDisplay();
  for (int d = 0; d < config->getNumDisplays(); ++d) {
    const std::string display = config->getDisplay(d);
    auto &views = policy.views[display];
    for (int v = 0; v < config->getNumViews(display.c_str()); ++v)
      views.push_back(config->getView(display.c_str(), v));
    policy.default_views[display] = config->getDefaultView(display.c_str());
  }
  return policy;
}
std::string rendering_identity(const OCIO::ConstConfigRcPtr &config, std::map<std::string, std::string> &files) {
  // OCIO may deduplicate processors with identical LUT contents. The shared
  // processor's metadata then describes only the first file, losing the other
  // dependencies from our watch ledger. Disable processor reuse only while
  // collecting this unpublished snapshot's identities and file dependencies.
  struct CacheFlags {
    const OCIO::ConstConfigRcPtr &config;
    OCIO::ProcessorCacheFlags saved;
    ~CacheFlags() { config->setProcessorCacheFlags(saved); }
  } flags{config, config->getProcessorCacheFlags()};
  config->setProcessorCacheFlags(OCIO::PROCESSOR_CACHE_OFF);
  ccl::MD5Hash hash;
  // Configuration-local input IDs also depend on the configuration name.
  hash.append(config->getName());
  hash.append("portable-color/3");
  // Displays, active view order and display transforms are presentation.
  // Colorspace definitions, roles, file rules and their actual processors
  // are rendering semantics. Processor IDs include resolved LUT contents.
  // Visibility controls menus, not saved graph validity. OCIO's no-argument
  // enumeration hides inactive spaces that getColorSpace still accepts.
  // Include every definition so a hidden input transform cannot remain stale.
  for (int i = 0; i < config->getNumColorSpaces(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL); ++i) {
    const auto space = config->getColorSpace(
        config->getColorSpaceNameByIndex(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL, i));
    std::ostringstream text;
    text << *space;
    hash.append(text.str());
    try {
      const auto processor = config->getProcessor(space->getName(), OCIO::ROLE_SCENE_LINEAR);
      hash.append(processor->getCacheID());
      const auto metadata = processor->getProcessorMetadata();
      for (int f = 0; f < metadata->getNumFiles(); ++f) {
        const std::string path = metadata->getFile(f);
        files[path] = file_revision(path);
      }
    } catch (const OCIO::Exception &error) {
      hash.append(std::string("unavailable:") + error.what());
    }
  }
  for (int i = 0; i < config->getNumRoles(); ++i) {
    const char *role = config->getRoleName(i);
    hash.append(role);
    hash.append(config->getRoleColorSpace(role));
  }
  std::ostringstream rules;
  rules << *config->getFileRules();
  hash.append(rules.str());
  return hash.get_hex();
}
} // namespace
std::shared_ptr<const ColorConfiguration> ColorConfigurationAccess::capture(const OCIO::ConstConfigRcPtr &current) {
  auto value = std::make_shared<ColorConfiguration::Impl>();
  auto frozen = current->createEditableCopy();
  frozen->clearProcessorCache();
  value->config = frozen;
  if (configuration_path_resolver)
    value->source_path = configuration_path_resolver();
  value->policy = describe(value->config);
  const auto output = working_interop_identity(value->config);
  value->policy.output_interop_id = output.id;
  value->policy.output_interop_selection = output.selection;
  value->portable = make_portable_color_configuration(value->config);
  value->policy.builtin_configuration = value->portable.builtin_configuration;
  std::map<std::string, std::string> files;
  value->policy.render_policy_id = rendering_identity(value->config, files);
  ccl::MD5Hash file_hash;
  for (const auto &[path, revision] : files) {
    file_hash.append(path);
    file_hash.append(revision);
  }
  value->policy.configuration_id += ":" + file_hash.get_hex();
  // Retain the compiled source transform, not only a config referring to
  // mutable LUT files. A later LUT replacement cannot change old RGB meaning.
  if (value->config->hasRole(OCIO::ROLE_SCENE_LINEAR) && value->config->hasRole("aces_interchange")) {
    value->to_interchange =
        value->config->getProcessor(value->policy.working_space.c_str(), "aces_interchange")->getDefaultCPUProcessor();
    value->from_interchange =
        value->config->getProcessor("aces_interchange", value->policy.working_space.c_str())->getDefaultCPUProcessor();
  }
  // Commit the watch ledger only after every required transform succeeds.
  // Failed reloads must retain the paths needed to detect file repair.
  input_file_revisions = std::move(files);
  return std::shared_ptr<const ColorConfiguration>(new ColorConfiguration(std::move(value)));
}
std::shared_ptr<const ColorConfiguration> current_color_configuration() {
  const auto config = OCIO::GetCurrentConfig();
  const std::string identity = config->getCacheID();
  std::lock_guard lock(snapshot_mutex);
  if (config != observed_config || identity != observed_identity || input_files_changed()) {
    // A newly selected config may reintroduce a LUT last watched by an older
    // config, so the current ledger alone cannot establish cache freshness.
    // Published frames retain compiled processors across this file-cache clear.
    OCIO::ClearAllCaches();
    auto captured = ColorConfigurationAccess::capture(config);
    observed_config = config;
    observed_identity = config->getCacheID();
    observed_snapshot = std::move(captured);
  }
  return observed_snapshot;
}
std::shared_ptr<const ColorConfiguration> presentation_color_configuration() {
  const auto config = OCIO::GetCurrentConfig();
  {
    std::lock_guard lock(snapshot_mutex);
    // Houdini publishes immutable configs. Disk freshness belongs to explicit
    // capture/start/update/manual checks and the registry's one-second poll.
    // Presentation and camera previews only consume the last accepted policy;
    // an explicit refresh immediately replaces the pointer returned here.
    if (observed_snapshot && observed_config == config)
      return observed_snapshot;
  }
  return current_color_configuration();
}
ColorPolicy color_policy() { return current_color_configuration()->policy(); }
OCIO::ConstConfigRcPtr owned_color_configuration(const ColorConfiguration &value) {
  return ColorConfigurationAccess::config(value);
}
void set_color_configuration_path_resolver(std::function<std::string()> resolver) {
  std::lock_guard lock(snapshot_mutex);
  configuration_path_resolver = std::move(resolver);
  observed_config.reset();
}
std::string color_configuration_path(const ColorConfiguration &value) { return ColorConfigurationAccess::path(value); }
bool synchronize_color_configuration() {
  const auto desired = current_color_configuration();
  std::lock_guard lock(active_mutex);
  if (synchronized_snapshot && synchronized_snapshot->policy().render_policy_id == desired->policy().render_policy_id)
    return false;
  synchronized_snapshot = desired;
  return true;
}
namespace {
std::shared_ptr<const ColorConfiguration> render_snapshot() {
  const auto config = OCIO::GetCurrentConfig();
  {
    std::lock_guard lock(snapshot_mutex);
    // Read-only renderer queries retain the policy already captured for this
    // config. Disk edits are validated by explicit host observation/update;
    // they must not prevent saving a frame whose processors are still owned.
    if (observed_snapshot && observed_config == config)
      return observed_snapshot;
  }
  return current_color_configuration();
}
} // namespace
std::string render_color_identity() { return render_snapshot()->policy().render_policy_id; }
OCIO::ConstConfigRcPtr render_color_configuration() {
  return ColorConfigurationAccess::portable(*render_snapshot()).config;
}
OCIO::ConstConfigRcPtr render_authoring_color_configuration() {
  return ColorConfigurationAccess::config(*render_snapshot());
}
PortableInputSpace render_portable_input_space(const std::string &id) {
  const auto value = render_snapshot();
  return portable_input_space(ColorConfigurationAccess::config(*value), ColorConfigurationAccess::portable(*value), id);
}
bool can_convert_working_pixels(const ColorConfiguration &source, const ColorConfiguration &target) {
  return source.policy().render_policy_id == target.policy().render_policy_id ||
         (ColorConfigurationAccess::interchange(source) && ColorConfigurationAccess::from_interchange(target));
}
void convert_working_pixels(const ColorConfiguration &source, const ColorConfiguration &target, float *pixels,
                            int width, int height, int channels) {
  if (source.policy().render_policy_id == target.policy().render_policy_id)
    return;
  if (width < 1 || height < 1 || (channels != 3 && channels != 4))
    throw std::invalid_argument("Invalid working-space image layout");
  const auto &from = ColorConfigurationAccess::interchange(source);
  const auto &to = ColorConfigurationAccess::from_interchange(target);
  if (!from || !to)
    throw PreviousColorConfiguration();
  const auto count = std::size_t(width) * height;
  if (channels == 4)
    for (std::size_t i = 0; i < count; ++i) {
      const float alpha = pixels[i * 4 + 3];
      // Zero coverage may carry additive emission. Unassociation leaves
      // those RGB values intact; a small positive coverage is still valid.
      if (alpha != 0)
        for (int c = 0; c < 3; ++c)
          pixels[i * 4 + c] /= alpha;
    }
  // A three-channel view of interleaved RGBA prevents arbitrary OCIO
  // transforms from modifying alpha, including in old-frame conversions.
  OCIO::PackedImageDesc image(pixels, width, height, 3, OCIO::BIT_DEPTH_F32, sizeof(float), channels * sizeof(float),
                              std::ptrdiff_t(width) * channels * sizeof(float));
  from->apply(image);
  to->apply(image);
  if (channels == 4)
    for (std::size_t i = 0; i < count; ++i) {
      const float alpha = pixels[i * 4 + 3];
      if (alpha != 0)
        for (int c = 0; c < 3; ++c)
          pixels[i * 4 + c] *= alpha;
    }
}
bool can_present_compact_display(const FrameEvent &frame, int width, int height) {
  // Restrict direct nearest sampling to integral pixel blocks. Non-integral
  // ratios need explicit expansion to keep their logical pixel coverage.
  return frame.x == 0 && frame.y == 0 && frame.width == frame.full_width && frame.height == frame.full_height &&
         frame.width % width == 0 && frame.height % height == 0 && (width != frame.width || height != frame.height);
}

DisplayImage display_image(const FrameEvent &frame, const DisplaySettings &settings, DisplayTimings *timings) {
  return display_image_with_storage(frame, settings, timings, nullptr);
}

DisplayImage display_image_with_storage(const FrameEvent &frame, const DisplaySettings &settings,
                                        DisplayTimings *timings, std::pair<int, int> *compact_size) {
  using Clock = std::chrono::steady_clock;
  const auto started = timings ? Clock::now() : Clock::time_point{};
  auto phase = started;
  const auto measure = [&](double DisplayTimings::*field) {
    if (timings) {
      const auto now = Clock::now();
      timings->*field = std::chrono::duration<double>(now - phase).count();
      phase = now;
    }
  };
  const auto plane =
      std::find_if(frame.planes.begin(), frame.planes.end(), [&](const auto &p) { return p.pass == settings.pass; });
  if (plane == frame.planes.end())
    throw std::invalid_argument("Pass is not available in this frame: " + settings.pass);
  if (settings.channels != "rgba" && settings.channels != "rgb" && settings.channels != "alpha")
    throw std::invalid_argument("Display channels must be rgba, rgb or alpha");
  if (settings.channels == "alpha" && plane->channels != 4)
    throw std::invalid_argument("Selected pass has no alpha channel");
  if (!std::isfinite(settings.exposure) || std::abs(settings.exposure) > 32)
    throw std::invalid_argument("Display exposure outside supported range");
  if (!std::isfinite(settings.gamma) || settings.gamma < 0.05f || settings.gamma > 8)
    throw std::invalid_argument("Display gamma must be within 0.05..8");
  const auto [width, height] = image_plane_size(frame, *plane);
  const bool compact = compact_size && can_present_compact_display(frame, width, height);
  if (compact_size)
    *compact_size = compact ? std::pair{width, height} : std::pair{frame.width, frame.height};
  const bool expand = !compact && (width != frame.width || height != frame.height);
  const auto count = static_cast<std::size_t>(width) * height;
  const auto &catalogue = frame.requested_passes;
  const auto descriptor =
      std::find_if(catalogue.begin(), catalogue.end(), [&](const auto &value) { return value.id == settings.pass; });
  if (descriptor != catalogue.end() && descriptor->cryptomatte && settings.channels == "alpha")
    throw std::invalid_argument("Cryptomatte RGBA contains ID/coverage pairs, not an alpha channel");
  const bool coverage = descriptor != catalogue.end() && descriptor->color;
  const bool color = coverage && settings.channels != "alpha";
  const auto exposure = std::exp2(settings.exposure);
  DisplayImage image;
  image.generation = frame.generation;
  image.sequence = frame.sequence;
  image.x = frame.x;
  image.y = frame.y;
  image.width = frame.width;
  image.height = frame.height;
  image.full_width = frame.full_width;
  image.full_height = frame.full_height;
  image.rgba.resize(count * 4);
  if (!color || settings.raw) {
    // Data/coverage and explicit raw views have no OCIO transform. Pack
    // directly from immutable storage instead of allocating and traversing
    // an additional full float RGBA image. Coverage belongs to color passes
    // even when alpha-only display deliberately disables color processing.
    measure(&DisplayTimings::prepare_seconds);
    const bool indexed = settings.pass == "object_id" || settings.pass == "material_id";
    const bool crypto = descriptor != catalogue.end() && descriptor->cryptomatte;
    const bool alpha_only = settings.channels == "alpha", rgba_selected = settings.channels == "rgba";
    const bool scalar = !color && !indexed && !crypto && plane->channels == 1;
    const auto pack = [](float v) {
      return std::uint8_t(std::lround(std::clamp(std::isfinite(v) ? v : 0.0f, 0.0f, 1.0f) * 255));
    };
    display_rows(width, height, [&](int begin, int end) {
      for (int y = begin; y < end; ++y)
        for (int x = 0; x < width; ++x) {
          const auto from = std::size_t(height - 1 - y) * width + x, to = (std::size_t(y) * width + x) * 4;
          if (alpha_only || scalar) {
            const auto value =
                alpha_only ? (plane->channels == 4 && coverage ? plane->pixels[from * 4 + 3] : 1) : plane->pixels[from];
            const auto byte = pack(value);
            image.rgba[to] = image.rgba[to + 1] = image.rgba[to + 2] = byte;
            image.rgba[to + 3] = 255;
            continue;
          }
          if (!color && !indexed && !crypto) {
            for (int c = 0; c < 3; ++c)
              image.rgba[to + c] = pack(plane->pixels[from * plane->channels + std::min(c, plane->channels - 1)]);
            image.rgba[to + 3] = 255;
            continue;
          }
          float values[4]{0, 0, 0, 1};
          if (indexed) {
            const auto index = std::uint32_t(std::max(0.f, plane->pixels[from]));
            auto hash = index * 0x9e3779b9u;
            hash ^= hash >> 16;
            hash *= 0x85ebca6bu;
            hash ^= hash >> 13;
            for (int c = 0; c < 3; ++c)
              values[c] = index ? .2f + .8f * float((hash >> (c * 8)) & 255) / 255.f : 0;
          } else if (crypto) {
            for (int pair = 0; pair < 2; ++pair) {
              const auto offset = from * 4 + pair * 2;
              const auto id = std::bit_cast<std::uint32_t>(plane->pixels[offset]);
              const auto weight = plane->pixels[offset + 1];
              if (weight <= 0)
                continue;
              for (int c = 0; c < 3; ++c)
                values[c] += weight * (.2f + .8f * float((id >> (c * 8)) & 255) / 255.f);
            }
          } else {
            const float alpha = plane->channels == 4 && coverage ? plane->pixels[from * 4 + 3] : 1;
            values[3] = rgba_selected ? alpha : 1;
            for (int c = 0; c < 3; ++c) {
              auto v = plane->pixels[from * plane->channels + std::min(c, plane->channels - 1)];
              if (color) {
                if (alpha != 0)
                  v /= alpha;
                v *= exposure;
              }
              values[c] = v;
            }
          }
          for (int c = 0; c < 4; ++c) {
            auto v = values[c];
            if (color && c < 3 && settings.gamma != 1 && std::isfinite(v))
              v = std::pow(std::max(0.0f, v), 1.0f / settings.gamma);
            image.rgba[to + c] = std::uint8_t(std::lround(std::clamp(std::isfinite(v) ? v : 0.0f, 0.0f, 1.0f) * 255));
          }
        }
    });
    if (expand)
      image.rgba = expand_display_pixels(image.rgba, width, height, frame.width, frame.height);
    measure(&DisplayTimings::pack_seconds);
    if (timings)
      timings->total_seconds = std::chrono::duration<double>(Clock::now() - started).count();
    return image;
  }
  // Viewer backgrounds: each adds an opaque composite in scene-linear light
  // before the view, View(exposure * RGB_premult + (1 - alpha) * B_linear),
  // stored after the straight image in the same buffer and transformed with it.
  const auto backgrounds = color ? display_backgrounds_linear(frame, settings) : std::vector<std::array<float, 3>>{};
  const auto layers = 1 + backgrounds.size();
  std::vector<float> pixels(count * 4 * layers);
  display_rows(width, height, [&](int begin, int end) {
    for (std::size_t i = std::size_t(begin) * width; i < std::size_t(end) * width; ++i) {
      if (settings.pass == "object_id" || settings.pass == "material_id") {
        const auto index = std::uint32_t(std::max(0.f, plane->pixels[i]));
        auto hash = index * 0x9e3779b9u;
        hash ^= hash >> 16;
        hash *= 0x85ebca6bu;
        hash ^= hash >> 13;
        for (int c = 0; c < 3; ++c)
          pixels[i * 4 + c] = index ? .2f + .8f * float((hash >> (c * 8)) & 255) / 255.f : 0;
        pixels[i * 4 + 3] = 1;
        continue;
      }
      if (descriptor != catalogue.end() && descriptor->cryptomatte) {
        // Preview ID/coverage pairs as stable false colors. Never interpret
        // encoded floating-point IDs as RGB, alpha, or OCIO input values.
        float values[3]{};
        for (int pair = 0; pair < 2; ++pair) {
          const auto offset = i * 4 + pair * 2;
          const auto id = std::bit_cast<std::uint32_t>(plane->pixels[offset]);
          const auto coverage = plane->pixels[offset + 1];
          if (coverage <= 0)
            continue;
          for (int c = 0; c < 3; ++c)
            values[c] += coverage * (.2f + .8f * float((id >> (c * 8)) & 255) / 255.f);
        }
        for (int c = 0; c < 3; ++c)
          pixels[i * 4 + c] = values[c];
        pixels[i * 4 + 3] = 1;
        continue;
      }
      const float alpha = plane->channels == 4 && color ? plane->pixels[i * 4 + 3] : 1;
      pixels[i * 4 + 3] = settings.channels == "rgba" ? alpha : 1;
      for (int c = 0; c < 3; ++c) {
        float v = plane->pixels[i * plane->channels + std::min(c, plane->channels - 1)];
        if (settings.channels == "alpha")
          v = alpha;
        else if (color) {
          // RGB without alpha is the image over black (associated RGB, as Nuke's
          // RGB view shows it); RGBA unassociates for the display-space blend.
          if (alpha != 0 && settings.channels == "rgba")
            v /= alpha;
          v *= exposure;
        }
        pixels[i * 4 + c] = v;
      }
      for (std::size_t k = 0; k < backgrounds.size(); ++k) {
        auto *to = pixels.data() + ((k + 1) * count + i) * 4;
        for (int c = 0; c < 3; ++c)
          to[c] = exposure * plane->pixels[i * 4 + c] + (1 - alpha) * backgrounds[k][c];
        to[3] = 1;
      }
    }
  });
  measure(&DisplayTimings::prepare_seconds);
  std::vector<OCIO::ConstCPUProcessorRcPtr> processors;
  if (color && !settings.raw) {
    const auto target = presentation_color_configuration();
    const auto &policy = target->policy();
    const auto &config = ColorConfigurationAccess::config(*target);
    if (!policy.views.empty()) {
      const auto display = settings.display.empty() ? policy.default_display : settings.display;
      const auto views = policy.views.find(display);
      if (views == policy.views.end())
        throw std::invalid_argument("Unknown OCIO display: " + display);
      const auto view = settings.view.empty() ? policy.default_views.at(display) : settings.view;
      if (std::find(views->second.begin(), views->second.end(), view) == views->second.end())
        throw std::invalid_argument("Unknown OCIO view: " + view);
      if (!frame.color_configuration)
        throw std::logic_error("Frame has no source color configuration");
      const auto &source = *frame.color_configuration;
      // Coverage belongs to the renderer. OCIO matrices may legitimately
      // transform alpha, so expose only RGB within the RGBA display buffer.
      if (source.policy().render_policy_id == policy.render_policy_id)
        processors.push_back(config
                                 ->getProcessor(source.policy().working_space.c_str(), display.c_str(), view.c_str(),
                                                OCIO::TRANSFORM_DIR_FORWARD)
                                 ->getDefaultCPUProcessor());
      else {
        const auto &source_processor = ColorConfigurationAccess::interchange(source);
        if (!source_processor || !config->hasRole("aces_interchange"))
          throw PreviousColorConfiguration();
        processors.push_back(source_processor);
        processors.push_back(
            config->getProcessor("aces_interchange", display.c_str(), view.c_str(), OCIO::TRANSFORM_DIR_FORWARD)
                ->getDefaultCPUProcessor());
      }
    } else if (!settings.display.empty() || !settings.view.empty()) {
      throw std::invalid_argument("OCIO display transforms are unavailable in the active configuration");
    }
  }
  measure(&DisplayTimings::processor_seconds);
  for (const auto &processor : processors)
    apply_display_processor(processor, pixels, width, height * int(layers));
  measure(&DisplayTimings::transform_seconds);
  image.composites.assign(backgrounds.size(), std::vector<std::uint8_t>(count * 4));
  for (std::size_t layer = 0; layer < layers; ++layer) {
    auto &output = layer ? image.composites[layer - 1] : image.rgba;
    const auto offset = layer * count * 4;
    display_rows(width, height, [&](int begin, int end) {
      for (int y = begin; y < end; ++y)
        for (int x = 0; x < width; ++x) {
          const auto from = offset + (static_cast<std::size_t>(height - 1 - y) * width + x) * 4;
          const auto to = (static_cast<std::size_t>(y) * width + x) * 4;
          for (int c = 0; c < 4; ++c) {
            auto v = pixels[from + c];
            if (color && c < 3 && settings.gamma != 1 && std::isfinite(v))
              v = std::pow(std::max(0.0f, v), 1.0f / settings.gamma);
            output[to + c] = std::uint8_t(std::lround(std::clamp(std::isfinite(v) ? v : 0.0f, 0.0f, 1.0f) * 255));
          }
          if (layer)
            output[to + 3] = 255;
        }
    });
  }
  if (expand) {
    image.rgba = expand_display_pixels(image.rgba, width, height, frame.width, frame.height);
    for (auto &composite : image.composites)
      composite = expand_display_pixels(composite, width, height, frame.width, frame.height);
  }
  measure(&DisplayTimings::pack_seconds);
  if (timings)
    timings->total_seconds = std::chrono::duration<double>(Clock::now() - started).count();
  return image;
}

std::vector<std::array<float, 3>> display_backgrounds_linear(const FrameEvent &frame, const DisplaySettings &settings,
                                                             std::string *method) {
  if (settings.backgrounds.empty() || settings.raw || settings.channels != "rgba" || !std::isfinite(settings.gamma) ||
      settings.gamma < .05f || settings.gamma > 8)
    return {};
  const auto descriptor = std::find_if(frame.requested_passes.begin(), frame.requested_passes.end(),
                                       [&](const auto &p) { return p.id == settings.pass; });
  if (descriptor == frame.requested_passes.end() || !descriptor->color || descriptor->cryptomatte)
    return {};
  const auto plane =
      std::find_if(frame.planes.begin(), frame.planes.end(), [&](const auto &p) { return p.pass == settings.pass; });
  if (plane == frame.planes.end() || plane->channels != 4)
    return {};
  // Undo the display gamma first: the viewer shows pow(View(x), 1 / gamma).
  std::vector<std::array<float, 3>> result;
  for (const auto &background : settings.backgrounds)
    result.push_back({std::pow(background[0] / 255.f, settings.gamma), std::pow(background[1] / 255.f, settings.gamma),
                      std::pow(background[2] / 255.f, settings.gamma)});
  const auto target = presentation_color_configuration();
  const auto &policy = target->policy();
  if (policy.views.empty()) {
    if (method)
      *method = "no display transform";
    return result;
  }
  // Another rendering policy goes through the interchange space; the viewer
  // keeps the display-space blend of the straight image for such old frames.
  if (!frame.color_configuration || frame.color_configuration->policy().render_policy_id != policy.render_policy_id)
    return {};
  const auto display = settings.display.empty() ? policy.default_display : settings.display;
  const auto views = policy.views.find(display);
  const auto default_view = policy.default_views.find(display);
  if (views == policy.views.end() || (settings.view.empty() && default_view == policy.default_views.end()))
    return {};
  const auto view = settings.view.empty() ? default_view->second : settings.view;
  if (std::find(views->second.begin(), views->second.end(), view) == views->second.end())
    return {};
  const auto &config = ColorConfigurationAccess::config(*target);
  OCIO::ConstCPUProcessorRcPtr inverse;
  std::string used = "view inverse";
  try {
    inverse =
        config->getProcessor(policy.working_space.c_str(), display.c_str(), view.c_str(), OCIO::TRANSFORM_DIR_INVERSE)
            ->getDefaultCPUProcessor();
  } catch (const OCIO::Exception &) {
    // A view without an inverse: invert only the display colour space.
    used = "display colour space inverse";
    try {
      const char *space = config->getDisplayViewColorSpaceName(display.c_str(), view.c_str());
      if (space && *space)
        inverse = config->getProcessor(space, policy.working_space.c_str())->getDefaultCPUProcessor();
    } catch (const OCIO::Exception &) {
    }
  }
  if (!inverse)
    return {};
  OCIO::ConstCPUProcessorRcPtr forward;
  if (used == "view inverse")
    forward =
        config->getProcessor(policy.working_space.c_str(), display.c_str(), view.c_str(), OCIO::TRANSFORM_DIR_FORWARD)
            ->getDefaultCPUProcessor();
  for (std::size_t index = 0; index < result.size(); ++index) {
    auto &value = result[index];
    inverse->applyRGB(value.data());
    if (!std::isfinite(value[0]) || !std::isfinite(value[1]) || !std::isfinite(value[2]))
      return {};
    // A view whose inverse is not exact (e.g. a gamut-compressing output
    // transform) can miss a byte: correct the working value until the view
    // shows the chosen bytes again, B += Vinv(target) - Vinv(View(B)).
    const auto goal = value;
    bool matched = !forward;
    for (int pass = 0; forward && pass < 8 && !matched; ++pass) {
      auto shown = value;
      forward->applyRGB(shown.data());
      bool exact = true;
      for (int c = 0; c < 3; ++c) {
        auto v = shown[c];
        if (settings.gamma != 1 && std::isfinite(v))
          v = std::pow(std::max(0.0f, v), 1.0f / settings.gamma);
        exact = exact && std::lround(std::clamp(std::isfinite(v) ? v : 0.0f, 0.0f, 1.0f) * 255) ==
                             settings.backgrounds[index][c];
      }
      if ((matched = exact))
        break;
      inverse->applyRGB(shown.data());
      for (int c = 0; c < 3; ++c)
        value[c] += goal[c] - shown[c];
    }
    if (!matched)
      value = goal; // Unreachable bytes: the plain inverse is the closest.
  }
  if (method)
    *method = used;
  return result;
}

std::shared_ptr<const DisplayGpuTransform> display_gpu_transform(const FrameEvent &frame,
                                                                 const DisplaySettings &settings) {
  if (settings.raw || settings.channels == "alpha")
    return {};
  const auto descriptor = std::find_if(frame.requested_passes.begin(), frame.requested_passes.end(),
                                       [&](const auto &p) { return p.id == settings.pass; });
  if (descriptor == frame.requested_passes.end() || !descriptor->color || descriptor->cryptomatte)
    return {};
  if (settings.channels != "rgba" && settings.channels != "rgb")
    return {};
  if (!std::isfinite(settings.exposure) || std::abs(settings.exposure) > 32 || !std::isfinite(settings.gamma) ||
      settings.gamma < .05f || settings.gamma > 8)
    return {};
  const auto plane =
      std::find_if(frame.planes.begin(), frame.planes.end(), [&](const auto &p) { return p.pass == settings.pass; });
  if (plane == frame.planes.end() || (plane->channels != 3 && plane->channels != 4))
    return {};
  const auto target = presentation_color_configuration();
  const auto &policy = target->policy();
  if (!frame.color_configuration || frame.color_configuration->policy().render_policy_id != policy.render_policy_id ||
      policy.views.empty())
    return {};
  const auto display = settings.display.empty() ? policy.default_display : settings.display;
  const auto views = policy.views.find(display);
  if (views == policy.views.end())
    return {};
  const auto view = settings.view.empty() ? policy.default_views.at(display) : settings.view;
  if (std::find(views->second.begin(), views->second.end(), view) == views->second.end())
    return {};
  ccl::MD5Hash hash;
  hash.append(policy.configuration_id);
  hash.append(policy.render_policy_id);
  hash.append(display);
  hash.append(view);
  const auto key = hash.get_hex();
  // One retained transform across display workers. Frames/configurations are
  // immutable; returned shared ownership survives a later cache replacement.
  static std::mutex mutex;
  static std::string cached_key;
  static std::shared_ptr<const DisplayGpuTransform> cached;
  std::lock_guard lock(mutex);
  if (cached_key == key)
    return cached;
  auto desc = OCIO::GpuShaderDesc::CreateShaderDesc();
  desc->setLanguage(OCIO::GPU_LANGUAGE_GLSL_4_0);
  desc->setFunctionName("cycles_view");
  desc->setResourcePrefix("cycles_");
  desc->setAllowTexture1D(false);
  ColorConfigurationAccess::config(*target)
      ->getProcessor(policy.working_space.c_str(), display.c_str(), view.c_str(), OCIO::TRANSFORM_DIR_FORWARD)
      ->getDefaultGPUProcessor()
      ->extractGpuShaderInfo(desc);
  cached_key = key;
  cached.reset();
  if (desc->getNumUniforms() || desc->getNum3DTextures())
    return {};
  auto result = std::make_shared<DisplayGpuTransform>();
  result->key = key;
  result->shader = desc->getShaderText();
  for (unsigned index = 0; index < desc->getNumTextures(); ++index) {
    const char *texture_name{}, *sampler{};
    unsigned width{}, height{};
    OCIO::GpuShaderCreator::TextureType channel;
    OCIO::GpuShaderCreator::TextureDimensions dimensions;
    OCIO::Interpolation interpolation;
    desc->getTexture(index, texture_name, sampler, width, height, channel, dimensions, interpolation);
    if (dimensions != OCIO::GpuShaderCreator::TEXTURE_2D)
      return {};
    const int channels = channel == OCIO::GpuShaderCreator::TEXTURE_RED_CHANNEL ? 1 : 3;
    const float *values{};
    desc->getTextureValues(index, values);
    result->textures.push_back({sampler, int(width), int(height), channels, interpolation != OCIO::INTERP_NEAREST,
                                std::vector<float>(values, values + std::size_t(width) * height * channels)});
  }
  cached = result;
  return result;
}
} // namespace cycles_houdini
