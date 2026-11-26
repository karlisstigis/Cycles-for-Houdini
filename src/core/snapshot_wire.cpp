// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/volume.h"
#include "mesh_payload.h"
#include <openvdb/io/Stream.h>
#include <openvdb/io/io.h>
#include <openvdb/tree/LeafManager.h>
#include "util/tbb.h"
#include <algorithm>
#include <atomic>
#include <bit>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <type_traits>

namespace cycles_houdini {
namespace {
constexpr std::uint64_t maximum_packet = 8ull * 1024 * 1024 * 1024;
constexpr std::uint32_t wire_magic = 0x43534857, wire_version = worker_protocol_version;
// Lowering-only hook exercises opaque stream sizes above the generic container
// ceiling without allocating a half-gigabyte fixture. Never raises that ceiling.
std::uint64_t container_limit() {
  constexpr std::uint64_t limit = 512ull * 1024 * 1024;
  if (const auto *text = std::getenv("CYCLES_HOUDINI_WIRE_TEST_CONTAINER_LIMIT"))
    return std::min<std::uint64_t>(limit, std::strtoull(text, nullptr, 10));
  return limit;
}
static_assert(std::endian::native == std::endian::little && sizeof(int) == 4 && sizeof(float) == 4);
static_assert(sizeof(Float2) == 8 && sizeof(Float3) == 12 && sizeof(Triangle) == 12 && sizeof(Matrix) == 48);
template <class> struct vector_type : std::false_type {};
template <class T> struct vector_type<std::vector<T>> : std::true_type {};
template <class T> struct vector_type<SharedArray<T>> : std::true_type {};
template <class> struct array_type : std::false_type {};
template <class T, std::size_t N> struct array_type<std::array<T, N>> : std::true_type {};
template <class> struct map_type : std::false_type {};
template <class K, class V> struct map_type<std::map<K, V>> : std::true_type {};
template <class> struct optional_type : std::false_type {};
template <class T> struct optional_type<std::optional<T>> : std::true_type {
  using element = T;
};
template <class> struct pointer_type : std::false_type {};
template <class T> struct pointer_type<std::shared_ptr<T>> : std::true_type {
  using element = std::remove_const_t<T>;
};
template <class T>
constexpr bool bulk = std::is_arithmetic_v<T> && !std::is_same_v<T, bool> || std::is_same_v<T, Float2> ||
                      std::is_same_v<T, Float3> || std::is_same_v<T, Triangle> || std::is_same_v<T, Matrix>;
// B1 (gathered encoding): small writes are staged, large arrays and volume
// leaf buffers are referenced; GatheredUpdate::write lays them out in order.
struct GatherSegment {
  enum Kind : std::uint8_t { staged, external, blocks };
  Kind kind{};
  std::uint64_t offset{}, size{}; // destination range, never empty
  const std::byte *source{};      // external
  std::uint64_t first{};          // staging offset (staged) or first block index (blocks)
  std::uint64_t block{};          // bytes per block (blocks)
};
struct Gather {
  std::vector<std::byte> staging;
  std::vector<GatherSegment> segments; // ascending, contiguous offsets
  std::vector<const std::byte *> blocks;
  std::uint64_t size{};
};
constexpr std::size_t gather_reference_bytes = 16 * 1024;
struct Writer {
  static constexpr bool reading = false;
  std::vector<std::byte> bytes;
  Gather *gather{};
  std::uint64_t size() const { return gather ? gather->size : bytes.size(); }
  const MeshPayloadSources *acknowledged{};
  bool reuse_mesh_payloads{true};
  struct MeshSource {
    const MeshData *mesh{};
    std::uint64_t index{};
    bool accepted{};
  };
  bool mesh_index_ready{};
  std::uint64_t mesh_count{};
  std::map<std::uint64_t, std::vector<MeshSource>> mesh_index;
  std::optional<MeshSource> mesh_source(const MeshData &value) {
    if (!reuse_mesh_payloads)
      return {};
    if (!mesh_index_ready) {
      mesh_index_ready = true;
      if (acknowledged)
        for (const auto &[key, mesh] : *acknowledged) {
          if (!mesh || mesh->key != key)
            throw std::invalid_argument("Invalid acknowledged mesh payload ownership");
          auto &candidates = mesh_index[mesh_payload_bucket(*mesh)];
          if (candidates.size() < 16)
            candidates.push_back({mesh.get(), 0, true});
        }
    }
    // The resource's own accepted version is the first partial-update base.
    // Cross-binding candidates remain bounded and require exact field masks.
    if (acknowledged) {
      const auto prior = acknowledged->find(value.key);
      if (prior != acknowledged->end())
        return MeshSource{prior->second.get(), 0, true};
    }
    auto &candidates = mesh_index[mesh_payload_bucket(value)];
    for (const auto &source : candidates)
      if (same_mesh_wire_payload(*source.mesh, value))
        return source;
    for (const auto &source : candidates)
      if (mesh_payload_equal(source.mesh->positions, value.positions) &&
          mesh_payload_equal(source.mesh->triangles, value.triangles))
        return source;
    return {};
  }
  void remember_mesh(const MeshData &value) {
    if (!reuse_mesh_payloads)
      return;
    auto &candidates = mesh_index[mesh_payload_bucket(value)];
    if (candidates.size() < 16)
      candidates.push_back({&value, mesh_count, false});
    ++mesh_count;
  }
  void raw(const void *data, std::size_t size) {
    if (size > maximum_packet - this->size())
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    if (gather) {
      if (!size)
        return;
      auto &g = *gather;
      const auto begin = g.staging.size();
      g.staging.resize(begin + size);
      std::memcpy(g.staging.data() + begin, data, size);
      if (!g.segments.empty() && g.segments.back().kind == GatherSegment::staged)
        g.segments.back().size += size; // contiguous in staging and destination
      else
        g.segments.push_back({GatherSegment::staged, g.size, size, nullptr, begin, 0});
      g.size += size;
      return;
    }
    const auto begin = bytes.size();
    bytes.resize(begin + size);
    if (size)
      std::memcpy(bytes.data() + begin, data, size);
  }
  // An array owned by the encoded update. Gathered encoding refers to it.
  void reference(const void *data, std::size_t size) {
    if (!gather || size < gather_reference_bytes)
      return raw(data, size);
    if (size > maximum_packet - this->size())
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    gather->segments.push_back(
        {GatherSegment::external, gather->size, size, static_cast<const std::byte *>(data), 0, 0});
    gather->size += size;
  }
  // Reserves a fixed slot that the caller fills (volume leaf values).
  std::byte *extend(std::size_t size) {
    if (size > maximum_packet - bytes.size())
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    const auto begin = bytes.size();
    bytes.resize(begin + size);
    return bytes.data() + begin;
  }
  // Reserve one contiguous staged range before independent fixed-slot writes.
  std::byte *stage(std::size_t count) {
    if (count > maximum_packet - size())
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    if (!gather)
      return extend(count);
    auto &g = *gather;
    const auto begin = g.staging.size();
    g.staging.resize(begin + count);
    if (count) {
      if (!g.segments.empty() && g.segments.back().kind == GatherSegment::staged)
        g.segments.back().size += count;
      else
        g.segments.push_back({GatherSegment::staged, g.size, count, nullptr, begin, 0});
      g.size += count;
    }
    return g.staging.data() + begin;
  }
  template <class T> void instances(const T &value);
  template <class T> void io(const T &value);
  template <class... T> void operator()(const T &...values) { (io(values), ...); }
};
// Worker decode: arrays of at least 8 MiB are copied out of the packet in
// 4 MiB pieces on all cores (same values; CYCLES_HOUDINI_SERIAL_CONVERSION=1
// keeps one memcpy). Measured on 30M triangles: decode 0.65 -> 0.37 s.
constexpr std::size_t parallel_decode_bytes = 8ull << 20;
void parallel_copy(std::byte *target, const std::byte *source, std::size_t size) {
  constexpr std::size_t piece = 4ull << 20;
  const auto pieces = (size + piece - 1) / piece;
  tbb::parallel_for(tbb::blocked_range<std::size_t>(0, pieces, 1), [=](const tbb::blocked_range<std::size_t> &r) {
    for (auto i = r.begin(); i != r.end(); ++i)
      std::memcpy(target + i * piece, source + i * piece, std::min(piece, size - i * piece));
  });
}
struct Reader {
  static constexpr bool reading = true;
  std::span<const std::byte> bytes;
  std::size_t position{};
  std::shared_ptr<const ColorConfiguration> color;
  const MeshPayloadSources *acknowledged{};
  std::vector<const MeshData *> mesh_sources;
  std::uint64_t allocated_bytes{}, allocation_limit{maximum_packet};
  const std::uint64_t container_size_limit{container_limit()};
  void allocate(std::uint64_t size) {
    if (size > allocation_limit - allocated_bytes)
      throw std::invalid_argument("Decoded worker allocation exceeds limit");
    allocated_bytes += size;
  }
  explicit Reader(std::span<const std::byte> input) : bytes(input) {
    if (input.size() > maximum_packet)
      throw std::invalid_argument("Worker packet exceeds allocation limit");
  }
  std::size_t remaining() const { return bytes.size() - position; }
  void raw(void *target, std::size_t size) {
    if (size > remaining())
      throw std::invalid_argument("Truncated worker packet");
    if (size)
      std::memcpy(target, bytes.data() + position, size);
    position += size;
  }
  std::size_t count() {
    std::uint64_t size{};
    io(size);
    if (size > remaining() || size > container_size_limit)
      throw std::invalid_argument("Worker container exceeds packet bounds");
    return std::size_t(size);
  }
  void finish() {
    if (remaining())
      throw std::invalid_argument("Trailing worker packet data");
  }
  template <class T> void io(T &value);
  template <class... T> void operator()(T &...values) { (io(values), ...); }
};
// Explicit field lists avoid dumping struct padding, engine pointers, callbacks
// or host handles. Order is part of this experimental versioned protocol.
#define CH_FIELDS(Type, ...)                                                                                           \
  template <class A, class T>                                                                                          \
    requires std::is_same_v<std::remove_cv_t<T>, Type>                                                                 \
  void fields(A &a, T &v) {                                                                                            \
    a(__VA_ARGS__);                                                                                                    \
  }
CH_FIELDS(RenderSession::OutputPass, v.source, v.layer, v.precision, v.part, v.compression, v.dwa_level)
CH_FIELDS(RenderSession::OutputAttribute, v.name, v.type, v.text, v.numbers)
CH_FIELDS(RenderSession::OutputOptions, v.half_color, v.crop_to_region, v.multipart, v.fail_on_missing_inputs,
          v.existing_files, v.compression, v.dwa_level, v.pixel_aspect, v.color_space, v.color_configuration, v.passes,
          v.alpha, v.metadata)
CH_FIELDS(WorkerOutput, v.filename, v.options)
CH_FIELDS(PreviewRequest, v.pass, v.focus)
CH_FIELDS(SourceDiagnostic, v.code, v.source, v.resource, v.message, v.action, v.severity, v.attribute)
CH_FIELDS(Float2, v.x, v.y)
CH_FIELDS(Float3, v.x, v.y, v.z)
CH_FIELDS(Triangle, v.a, v.b, v.c)
CH_FIELDS(EdgeCrease, v.a, v.b, v.sharpness)
CH_FIELDS(VertexCrease, v.point, v.sharpness)
CH_FIELDS(ShaderEndpoint, v.node, v.socket)
CH_FIELDS(ShaderLink, v.from, v.to)
CH_FIELDS(ShaderNodeData, v.id, v.definition, v.parameters)
CH_FIELDS(MaterialSettings, v.emission_sampling, v.transparent_shadows, v.bump_map_correction, v.volume_sampling,
          v.volume_interpolation, v.volume_step_rate)
CH_FIELDS(ShaderGraphData, v.schema_version, v.nodes, v.links, v.surface, v.volume, v.displacement,
          v.displacement_method, v.displacement_time_dependent, v.image_time, v.settings)
CH_FIELDS(MaterialData, v.key, v.kind, v.color, v.roughness, v.metallic, v.emission_strength, v.density, v.anisotropy,
          v.volume_emission_strength, v.volume_emission_color, v.emission_field, v.blackbody_intensity,
          v.temperature_kelvin_scale, v.color_attribute, v.graph, v.cryptomatte_name, v.pass_index,
          v.source_diagnostics, v.displacement_samples)
CH_FIELDS(GeometryMotion, v.positions, v.radii)
CH_FIELDS(NamedAttribute, v.name, v.components, v.values, v.motion_values, v.motion_complete)
CH_FIELDS(UniformAttribute, v.name, v.components, v.value, v.motion_values, v.motion_complete)
CH_FIELDS(SubdivisionSettings, v.type, v.space, v.boundary, v.fvar, v.dicing_rate, v.max_level, v.triangle_limit,
          v.adaptive, v.use_creases, v.cache_topology)
CH_FIELDS(SubdivisionData, v.settings, v.face_sizes, v.face_corners, v.face_material, v.face_provenance, v.edge_creases,
          v.vertex_creases)
CH_FIELDS(MeshNominalPose, v.positions, v.corner_normals, v.corner_uv, v.corner_colors, v.attributes)
CH_FIELDS(GaussianSplatData, v.scene_linear_color, v.scales, v.rotations, v.radiance, v.harmonics, v.motion_scales,
          v.motion_rotations, v.motion_radiance)
CH_FIELDS(PointData, v.key, v.revision, v.material, v.materials, v.material_slots, v.positions, v.radii, v.colors,
          v.randoms, v.uv, v.lifetimes, v.splats, v.motion, v.generated_attribute, v.attributes, v.motion_colors,
          v.motion_uv, v.motion_color_complete, v.motion_uv_complete, v.instance_attributes)
CH_FIELDS(CurveData, v.key, v.revision, v.material, v.materials, v.material_slots, v.positions, v.radii, v.first_key,
          v.colors, v.randoms, v.uv, v.motion, v.shape, v.generated, v.generated_attribute, v.attributes,
          v.motion_colors, v.motion_uv, v.motion_color_complete, v.motion_uv_complete, v.instance_attributes)
CH_FIELDS(ObjectProperties, v.holdout, v.caustics_caster, v.caustics_receiver, v.shadow_terminator_shading,
          v.shadow_terminator_geometry, v.ao_distance, v.lightgroup, v.cryptomatte_object, v.cryptomatte_asset,
          v.pass_index, v.shadow_catcher, v.linking_id, v.dielectric_priority, v.color, v.alpha)
CH_FIELDS(ObjectData, v.key, v.geometry_key, v.transform, v.visibility, v.random_id, v.motion, v.properties,
          v.nominal_transform, v.attributes)
CH_FIELDS(ObjectAttributeData, v.key, v.attributes, v.pose)
CH_FIELDS(LightCone, v.inner_half_angle, v.outer_half_angle, v.rolloff, v.reverse_axis)
CH_FIELDS(LightData, v.key, v.kind, v.transform, v.strength, v.radius, v.size_u, v.size_v, v.angle, v.normalize,
          v.cast_shadow, v.use_mis, v.visibility, v.ellipse, v.is_sphere, v.use_caustics, v.spread, v.spot_smooth,
          v.max_bounces, v.cone, v.lightgroup, v.cryptomatte_object, v.cryptomatte_asset, v.shadow_catcher, v.graph,
          v.source_path, v.linking_id, v.is_portal)
CH_FIELDS(CameraSampling, v.rolling_shutter, v.rolling_duration, v.shutter_curve)
CH_FIELDS(PanoramaSettings, v.type, v.fisheye_fov, v.fisheye_lens, v.latitude_min, v.latitude_max, v.longitude_min,
          v.longitude_max, v.polynomial, v.cylindrical_u_min, v.cylindrical_u_max, v.cylindrical_v_min,
          v.cylindrical_v_max, v.sensor_width, v.sensor_height, v.sensor_fit)
CH_FIELDS(CameraDofSample, v.aperture_size, v.focus_distance, v.aperture_rotation, v.aperture_ratio)
CH_FIELDS(CameraIntrinsicSample, v.short_axis_fov, v.near_clip, v.far_clip, v.ortho_scale, v.panorama)
CH_FIELDS(CameraData, v.pixel_aspect, v.transform, v.short_axis_fov, v.near_clip, v.far_clip, v.orthographic,
          v.ortho_scale, v.aperture_size, v.focus_distance, v.aperture_rotation, v.aperture_ratio, v.aperture_blades,
          v.image_iris, v.image_iris_method, v.image_iris_color, v.dof_motion, v.intrinsic_motion, v.shutter_frames,
          v.motion, v.sampling, v.panorama, v.source_diagnostics)
CH_FIELDS(DicingSettings, v.offscreen_scale, v.camera, v.update, v.refresh_revision, v.topology_cache_mib)
CH_FIELDS(Crop, v.x, v.y, v.width, v.height)
CH_FIELDS(DenoiseSettings, v.enabled, v.method, v.use_gpu, v.quality, v.prefilter, v.start_sample, v.passes,
          v.upscale_factor)
CH_FIELDS(RenderSettings, v.samples, v.adaptive_sampling, v.noise_threshold, v.min_samples, v.time_limit,
          v.sampling_pattern, v.seed, v.animate_seed, v.frame, v.min_bounce, v.max_bounce, v.diffuse_bounce,
          v.glossy_bounce, v.transmission_bounce, v.volume_bounce, v.transparent_min_bounce, v.transparent_max_bounce,
          v.light_tree, v.light_threshold, v.clamp_direct, v.clamp_indirect, v.filter_glossy, v.glossy_lobe_floor,
          v.resampled_light_sampling, v.use_guiding, v.deterministic_guiding, v.nested_dielectrics,
          v.caustics_reflective, v.caustics_refractive, v.fast_gi, v.fast_gi_method, v.ao_bounces, v.ao_factor,
          v.ao_distance, v.volume_biased, v.volume_step_rate, v.volume_max_steps, v.film_exposure, v.filter_width,
          v.pass_alpha_threshold, v.pixel_filter, v.sample_subset, v.subset_offset, v.subset_length, v.mist_start,
          v.mist_depth, v.mist_falloff)
CH_FIELDS(AovDefinition, v.name, v.color)
CH_FIELDS(WorldData, v.graph, v.visibility, v.use_mis, v.cast_shadow, v.use_caustics, v.map_resolution, v.max_bounces,
          v.volume_sampling, v.source_path)
CH_FIELDS(CryptomatteSettings, v.object, v.material, v.asset, v.levels)
CH_FIELDS(LinkingRule, v.objects, v.emitters, v.exclude)
CH_FIELDS(LinkingSettings, v.receivers, v.blockers)
CH_FIELDS(SceneUpdate, v.source_diagnostics, v.denoising, v.rendering, v.materials, v.meshes, v.volumes, v.points,
          v.curves, v.objects, v.object_poses, v.object_attributes, v.lights, v.deleted_objects, v.deleted_geometry,
          v.deleted_materials, v.deleted_lights, v.camera, v.dicing, v.crop, v.background, v.background_lightgroup,
          v.transparent_background, v.transparent_glass, v.transparent_roughness_threshold, v.motion_blur, v.passes,
          v.aovs, v.lightgroups, v.cryptomatte, v.defer_camera_subdivision, v.background_shadow_catcher, v.world,
          v.linking)
CH_FIELDS(TextureHeader, v.filename, v.file_colorspace, v.color_interop_id, v.format, v.chromaticities,
          v.aces_image_container)
CH_FIELDS(TextureInputSelection, v.colorspace, v.reason, v.label)
CH_FIELDS(TexturePolicyEntry, v.header, v.selection)
CH_FIELDS(OwnedTexturePolicy, v.identity, v.entries)
CH_FIELDS(TextureImageKey, v.source, v.evaluation_time)
CH_FIELDS(TextureImageData, v.source, v.revision, v.colorspace, v.error, v.width, v.height, v.channels,
          v.evaluation_time, v.unassociated_alpha, v.pixels)
CH_FIELDS(TextureInputInfo, v.material_key, v.filename, v.colorspace, v.selection, v.policy_id, v.revision, v.tiles,
          v.portable_decode_bytes, v.portable_cache_format)
CH_FIELDS(OwnedColorPolicy, v.config_path, v.yaml, v.working_directory, v.render_policy_id, v.context)
CH_FIELDS(SessionConfig, v.device_id, v.device_ids, v.width, v.height, v.samples, v.threads, v.interactive,
          v.progressive_display, v.use_tiling, v.tile_size, v.persistent_updates, v.collect_diagnostics,
          v.use_texture_cache, v.auto_texture_cache, v.hair_subdivisions, v.refit_meshes, v.passes, v.aovs,
          v.lightgroups, v.cryptomatte, v.denoising)
CH_FIELDS(WorkerConfiguration, v.session, v.color, v.resource_root)
CH_FIELDS(PoseOriginRecord, v.key, v.root, v.local, v.motion, v.nominal)
CH_FIELDS(RootPoseRecord, v.key, v.centre, v.nominal, v.motion)
CH_FIELDS(RootPoseUpdate, v.token, v.reset_origins, v.origins, v.roots)
CH_FIELDS(WorkerUpdate, v.scene, v.textures, v.color, v.reuse_if_unchanged, v.image_upserts, v.deleted_images,
          v.root_poses)
CH_FIELDS(PassDescriptor, v.id, v.channels, v.storage, v.filtered, v.color, v.denoise_eligible, v.cryptomatte,
          v.internal)
CH_FIELDS(ImagePlane, v.pass, v.channels, v.pixels, v.width, v.height)
CH_FIELDS(SessionStatus, v.image_iris_preparations, v.image_iris_table_bytes, v.image_iris_preparation_seconds,
          v.source_diagnostics, v.state, v.generation, v.current_sample, v.geometry_conversions, v.object_updates,
          v.direct_attribute_updates, v.expanded_attribute_updates, v.attribute_fallback_count,
          v.attribute_fallback_reason, v.unchanged_reuses, v.rendering, v.message, v.error, v.render_device_ids,
          v.progress, v.elapsed_seconds, v.first_image_seconds, v.last_update_seconds, v.owned_geometry_bytes,
          v.device_allocation_bytes, v.device_allocation_peak_bytes, v.geometry_resources, v.object_resources,
          v.material_resources, v.light_resources, v.engine_shader_resources, v.retired_shader_resources,
          v.shader_reuses, v.mesh_variant_resources, v.mesh_variant_bytes, v.retained_mesh_source_bytes,
          v.mesh_family_resources, v.mesh_family_members, v.mesh_variant_bindings, v.mesh_metric_records,
          v.displacement_rebuilds, v.mesh_refits, v.subdivision_control_faces, v.subdivision_vertices,
          v.subdivision_triangles, v.subdivision_tessellations, v.subdivision_topology_cache_hits,
          v.subdivision_topology_cache_misses, v.subdivision_topology_cache_admissions,
          v.subdivision_topology_cache_evictions, v.subdivision_topology_cache_oversized,
          v.subdivision_topology_cache_entries, v.subdivision_topology_cache_bytes,
          v.subdivision_topology_cache_budget_bytes, v.subdivision_topology_cache_active_retired_bytes,
          v.volume_vector_merges, v.volume_vector_fields, v.volume_vector_source_bytes, v.volume_ray_marching,
          v.color_update_pending, v.color_epoch, v.color_target_epoch, v.denoiser, v.denoise_device_id,
          v.engine_nested_dielectrics, v.dielectric_priority_objects, v.dielectric_priority_min,
          v.dielectric_priority_max, v.nested_probe_unresolved, v.nested_stack_depth_clamped, v.nested_stack_depth)
CH_FIELDS(BufferCopyDiagnostic, v.calls, v.requested_bytes, v.host_seconds)
CH_FIELDS(DeviceUpdateMemoryDevice, v.id, v.physical_id, v.backend, v.description, v.start_bytes, v.peak_bytes,
          v.end_bytes, v.max_build_temp_bytes, v.max_build_temp_allocation_bytes, v.build_count,
          v.reused_temp_build_count, v.local_temp_build_count, v.kept_temp_bytes_before_attributes)
CH_FIELDS(DeviceUpdateMemory, v.available, v.update_serial, v.generation, v.outcome, v.scope, v.devices)
CH_FIELDS(SessionDiagnostics, v.enabled, v.generation, v.display_updates, v.output_deliveries, v.core_update_seconds,
          v.cancel_join_seconds, v.display_conversion_seconds, v.output_delivery_seconds, v.scene_boundary_wait_seconds,
          v.session_cancel_calls, v.shader_graph_preparation_seconds, v.volume_vector_assembly_seconds,
          v.cancel_output_deliveries, v.cancel_display_updates, v.cancel_output_delivery_seconds,
          v.cancel_display_conversion_seconds, v.output_reads_skipped, v.display_updates_skipped,
          v.cancel_output_reads_skipped, v.cancel_display_updates_skipped, v.engine_statistics_pending,
          v.engine_snapshot_wait_seconds, v.material_builds, v.light_updates, v.background_builds,
          v.engine_update_seconds, v.buffer_copy_requests, v.output_plane_reads, v.device_update_memory)
CH_FIELDS(DeviceDescriptor, v.id, v.name, v.backend, v.display_device, v.denoisers, v.physical_id)
CH_FIELDS(WorkerReply, v.success, v.error, v.update_rejected, v.renderer_pid, v.status, v.diagnostics, v.textures,
          v.textures_changed, v.process_memory, v.frame, v.focus_frame, v.focus_is_frame, v.devices)
#undef CH_FIELDS
template <class A>
void mesh_binding_indices(A &a, std::conditional_t<A::reading, std::vector<int>, const std::vector<int>> &indices,
                          std::size_t faces) {
  if constexpr (A::reading) {
    std::uint8_t tag{};
    a(tag);
    if (tag == 0)
      indices.clear();
    else if (tag == 1) {
      if (!faces)
        throw std::invalid_argument("Constant mesh binding requires owned faces");
      int value{};
      a(value);
      a.allocate(std::uint64_t(faces) * sizeof(int));
      indices.assign(faces, value);
    } else if (tag == 2)
      a(indices);
    else
      throw std::invalid_argument("Invalid mesh binding index tag");
  } else {
    const bool constant = !indices.empty() && indices.size() == faces &&
                          std::all_of(indices.begin() + 1, indices.end(), [&](int i) { return i == indices.front(); });
    const std::uint8_t tag = indices.empty() ? 0 : constant ? 1 : 2;
    a(tag);
    if (tag == 1)
      a(indices.front());
    else if (tag == 2)
      a(indices);
  }
}
template <class A, class Current, class Base>
void mesh_delta_fields(A &a, Current values, const Base &base, bool has_base);

template <class A, class T> void mesh_named_fields(A &a, T &values, const std::vector<NamedAttribute> *base) {
  if constexpr (A::reading) {
    const auto count = a.count();
    a.allocate(std::uint64_t(count) * sizeof(NamedAttribute));
    values.resize(count);
    for (auto &value : values) {
      std::uint64_t index{};
      a(value.name, index);
      const NamedAttribute *source{};
      if (index != std::numeric_limits<std::uint64_t>::max()) {
        if (!base || index >= base->size() || (*base)[index].name != value.name)
          throw std::invalid_argument("Invalid named mesh field reference");
        source = &(*base)[index];
        if (std::count_if(base->begin(), base->end(), [&](const auto &entry) { return entry.name == value.name; }) != 1)
          throw std::invalid_argument("Ambiguous named mesh field reference");
      }
      const NamedAttribute empty;
      const auto &old = source ? *source : empty;
      mesh_delta_fields(a, std::tie(value.components, value.values, value.motion_values, value.motion_complete),
                        std::tie(old.components, old.values, old.motion_values, old.motion_complete),
                        source != nullptr);
    }
  } else {
    a(std::uint64_t(values.size()));
    for (const auto &value : values) {
      std::uint64_t index = std::numeric_limits<std::uint64_t>::max();
      const NamedAttribute *source{};
      if (base)
        for (std::size_t i = 0; i < base->size(); ++i)
          if ((*base)[i].name == value.name) {
            if (source) {
              source = nullptr;
              index = std::numeric_limits<std::uint64_t>::max();
              break;
            }
            source = &(*base)[i];
            index = i;
          }
      a(value.name, index);
      const NamedAttribute empty;
      const auto &old = source ? *source : empty;
      mesh_delta_fields(a, std::tie(value.components, value.values, value.motion_values, value.motion_complete),
                        std::tie(old.components, old.values, old.motion_values, old.motion_complete),
                        source != nullptr);
    }
  }
}

template <class A, class Current, class Base>
void mesh_delta_fields(A &a, Current values, const Base &base, bool has_base) {
  constexpr auto size = std::tuple_size_v<Current>;
  static_assert(size > 0 && size < 64);
  constexpr std::uint64_t all = (std::uint64_t(1) << size) - 1;
  std::uint64_t mask{};
  if constexpr (A::reading) {
    a(mask);
    if ((mask & ~all) || (!has_base && mask != all))
      throw std::invalid_argument("Invalid mesh field reuse mask");
  } else {
    mask = has_base ? mesh_changed_mask(values, base) : all;
    a(mask);
  }
  [&]<std::size_t... I>(std::index_sequence<I...>) {
    const auto field = [&]<std::size_t J>() {
      auto &value = std::get<J>(values);
      const auto &source = std::get<J>(base);
      if (mask & (std::uint64_t(1) << J)) {
        if constexpr (std::is_same_v<std::remove_cvref_t<decltype(value)>, std::vector<NamedAttribute>>)
          mesh_named_fields(a, value, has_base ? &source : nullptr);
        else
          a(value);
      } else if constexpr (A::reading) {
        // Bound logical retained payload as well as new allocations:
        // a small packet cannot amplify references without limit.
        a.allocate(mesh_payload_heap(source));
        value = source;
      }
    };
    (field.template operator()<I>(), ...);
  }(std::make_index_sequence<size>{});
}

template <class A, class T> void mesh_payload_fields(A &a, T &v, const MeshData *source) {
  const MeshData empty;
  const auto &old = source ? *source : empty;
  mesh_delta_fields(a, mesh_wire_members(v), mesh_wire_members(old), source != nullptr);
  if constexpr (A::reading) {
    bool present{};
    a(present);
    if (present)
      v.nominal_pose.emplace();
    else
      v.nominal_pose.reset();
  } else
    a(bool(v.nominal_pose));
  if (v.nominal_pose) {
    const MeshNominalPose empty_pose;
    const auto &prior = old.nominal_pose ? *old.nominal_pose : empty_pose;
    mesh_delta_fields(a, nominal_wire_members(*v.nominal_pose), nominal_wire_members(prior),
                      source && bool(old.nominal_pose));
  }
  if constexpr (A::reading) {
    bool present{};
    a(present);
    if (present)
      v.subdivision.emplace();
    else
      v.subdivision.reset();
  } else
    a(bool(v.subdivision));
  if (v.subdivision) {
    const SubdivisionData empty_subd;
    const auto &prior = old.subdivision ? *old.subdivision : empty_subd;
    mesh_delta_fields(a, subdivision_payload_members(*v.subdivision), subdivision_payload_members(prior),
                      source && bool(old.subdivision));
  }
}

template <class A, class T>
  requires std::is_same_v<std::remove_cv_t<T>, MeshData>
void fields(A &a, T &v) {
  a(v.key, v.revision);
  if constexpr (A::reading) {
    std::uint8_t tag{};
    a(tag);
    const MeshData *source{};
    std::uint64_t revision{};
    if (tag == 1) {
      std::uint64_t index{};
      a(index, revision);
      if (index >= a.mesh_sources.size())
        throw std::invalid_argument("Invalid forward mesh payload reference");
      source = a.mesh_sources[std::size_t(index)];
    } else if (tag == 2) {
      std::string key;
      a(key, revision);
      if (!a.acknowledged)
        throw std::invalid_argument("Mesh payload reference requires an acknowledged scene");
      const auto found = a.acknowledged->find(key);
      if (found == a.acknowledged->end() || !found->second || found->second->key != key)
        throw std::invalid_argument("Unknown acknowledged mesh payload reference");
      source = found->second.get();
    } else if (tag != 0)
      throw std::invalid_argument("Invalid mesh payload reference tag");
    if (source && source->revision != revision)
      throw std::invalid_argument("Stale mesh payload reference revision");
    mesh_payload_fields(a, v, source);
    a.mesh_sources.push_back(&v);
  } else {
    const auto source = a.mesh_source(v);
    const std::uint8_t tag = !source ? 0 : source->accepted ? 2 : 1;
    a(tag);
    if (source) {
      if (tag == 2)
        a(source->mesh->key);
      else
        a(source->index);
      a(source->mesh->revision);
    }
    mesh_payload_fields(a, v, source ? source->mesh : nullptr);
    a.remember_mesh(v);
  }
  // Neither palette identity nor face assignments are borrowed from a source.
  a(v.materials);
  mesh_binding_indices(a, v.triangle_material, v.triangles.size());
  if (v.subdivision)
    mesh_binding_indices(a, v.subdivision->face_material, v.subdivision->face_sizes.size());
  a(v.instance_attributes);
}

template <class A, class T>
  requires std::is_same_v<std::remove_cv_t<T>, ObjectPoseData>
void fields(A &a, T &v) {
  a(v.key, v.transform, v.motion);
  // Requested-frame recovery often has exactly the current transform. Keep
  // its presence, but avoid repeating that matrix. Bit comparison preserves
  // signed zero and every authored float bit; other matrices remain explicit.
  if constexpr (A::reading) {
    std::uint8_t tag{};
    a(tag);
    if (tag == 0)
      v.nominal_transform.reset();
    else if (tag == 1)
      v.nominal_transform = v.transform;
    else if (tag == 2) {
      Matrix nominal;
      a(nominal);
      v.nominal_transform = nominal;
    } else
      throw std::invalid_argument("Invalid object pose nominal-transform tag");
  } else {
    const std::uint8_t tag = !v.nominal_transform                                                                ? 0
                             : std::memcmp(v.transform.data(), v.nominal_transform->data(), sizeof(Matrix)) == 0 ? 1
                                                                                                                 : 2;
    a(tag);
    if (tag == 2)
      a(*v.nominal_transform);
  }
}
// Volume fields (protocol 88). A float or vec3-float grid travels as its
// metadata, transform and OpenVDB topology (uncompressed node masks and tile
// values) followed by every leaf buffer verbatim, active and inactive values,
// in LeafManager order. Leaf values are copied in parallel into fixed slots,
// so the bytes do not depend on scheduling; chunks hold at most 64 Mi floats.
// Other grid types, grids stored as half floats and grids with unallocated
// in-core leaves keep the OpenVDB stream form (tag 0) used before.
// The chunk size travels with the field and the reader accepts any size up to
// the limit. Test hooks: CYCLES_HOUDINI_VOLUME_WIRE_STREAM=1 forces the stream
// form (comparing the two forms) and CYCLES_HOUDINI_VOLUME_WIRE_CHUNK_LEAVES=n
// writes smaller chunks (exercising chunk boundaries with small grids).
// CYCLES_HOUDINI_SERIAL_CONVERSION=1 copies on the calling thread only.
enum : std::uint8_t { volume_wire_stream = 0, volume_wire_float = 1, volume_wire_vector = 2 };
constexpr std::uint64_t volume_wire_chunk_floats = 64ull * 1024 * 1024;
bool volume_wire_env(const char *name) {
  const auto *value = std::getenv(name);
  return value && std::string_view(value) == "1";
}
template <class Grid> constexpr std::size_t volume_wire_leaves_per_chunk() {
  using Leaf = typename Grid::TreeType::LeafNodeType;
  constexpr std::size_t floats = sizeof(typename Grid::ValueType) / sizeof(float);
  static_assert(sizeof(typename Grid::ValueType) == floats * sizeof(float));
  return std::size_t(volume_wire_chunk_floats / (floats * Leaf::SIZE));
}
template <class F> void volume_wire_leaf_ranges(std::size_t count, bool serial, const F &f) {
  constexpr std::size_t grain = 512;
  if (serial || count <= grain)
    f(std::size_t(0), count);
  else
    tbb::parallel_for(tbb::blocked_range<std::size_t>(0, count, grain),
                      [&](const tbb::blocked_range<std::size_t> &range) { f(range.begin(), range.end()); });
}
// An artist-facing refusal for a field whose voxels cannot fit in one update,
// raised before any voxel is copied. CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT can
// only lower the limit (tests exercise this check with small grids).
template <class Grid>
void check_volume_wire_size(const Writer &a, const Grid &grid, std::size_t leaves, std::size_t header) {
  using Leaf = typename Grid::TreeType::LeafNodeType;
  constexpr double voxel_bytes = double(sizeof(typename Grid::ValueType)) + double(Leaf::NUM_VALUES / 8) / Leaf::SIZE;
  std::uint64_t limit = maximum_packet;
  if (const auto *text = std::getenv("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT"))
    limit = std::min<std::uint64_t>(limit, std::strtoull(text, nullptr, 10));
  const auto available = limit > a.size() ? limit - a.size() : 0;
  const auto needed = double(leaves) * Leaf::SIZE * sizeof(typename Grid::ValueType) + double(header) + 64.0;
  if (needed <= double(available))
    return;
  const auto gb = [](double bytes) {
    std::ostringstream text;
    text.precision(bytes < 1e9 ? 3 : 2);
    if (bytes < 1e9)
      text << bytes / 1e6 << " MB";
    else
      text << bytes / 1e9 << " GB";
    return text.str();
  };
  const auto side = std::uint64_t(std::cbrt(double(limit) / voxel_bytes));
  throw std::invalid_argument("Volume field '" + grid.getName() + "' is too large to send to the renderer: its " +
                              std::to_string(std::uint64_t(leaves) * Leaf::SIZE) + " stored voxels need " + gb(needed) +
                              ", and one scene update can carry at most " + gb(double(limit)) + " in total (about " +
                              std::to_string(side) +
                              "^3 voxels of this type). Reduce the "
                              "resolution, or deactivate and prune empty regions of the VDB.");
}
template <class Grid> bool write_volume_grid(Writer &a, const Grid &grid, std::uint8_t tag) {
  using Tree = typename Grid::TreeType;
  using Leaf = typename Tree::LeafNodeType;
  using Value = typename Grid::ValueType;
  if (grid.saveFloatAsHalf())
    return false;
  const bool serial = volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION");
  const openvdb::tree::LeafManager<const Tree> leaves(grid.tree(), 0, serial);
  const std::size_t count = leaves.leafCount();
  for (std::size_t i = 0; i < count; ++i) {
    const auto &buffer = leaves.leaf(i).buffer();
    if (buffer.empty() && !buffer.isOutOfCore())
      return false;
  }
  std::ostringstream header(std::ios::binary);
  openvdb::io::setDataCompression(header, 0);
  grid.writeMeta(header);
  grid.transform().write(header);
  grid.tree().writeTopology(header, false);
  check_volume_wire_size(a, grid, count, std::size_t(header.tellp()));
  std::size_t per_chunk = volume_wire_leaves_per_chunk<Grid>();
  if (const auto *text = std::getenv("CYCLES_HOUDINI_VOLUME_WIRE_CHUNK_LEAVES"))
    per_chunk = std::clamp<std::size_t>(std::strtoull(text, nullptr, 10), 1, per_chunk);
  a(tag, header.str(), std::uint64_t(count), std::uint64_t(per_chunk));
  constexpr std::size_t leaf_bytes = Leaf::SIZE * sizeof(Value);
  for (std::size_t begin = 0; begin < count; begin += per_chunk) {
    const auto size = std::min(per_chunk, count - begin);
    a(std::uint64_t(size * Leaf::SIZE));
    if (a.gather) {
      // Same bytes, written later by GatheredUpdate::write from these leaves.
      if (size * leaf_bytes > maximum_packet - a.size())
        throw std::invalid_argument("Worker packet exceeds allocation limit");
      auto &g = *a.gather;
      const auto first = g.blocks.size();
      g.blocks.resize(first + size);
      auto *slots = g.blocks.data() + first;
      volume_wire_leaf_ranges(size, serial, [&](std::size_t from, std::size_t to) {
        for (auto i = from; i < to; ++i)
          slots[i] = reinterpret_cast<const std::byte *>(leaves.leaf(begin + i).buffer().data());
      });
      g.segments.push_back({GatherSegment::blocks, g.size, size * leaf_bytes, nullptr, first, leaf_bytes});
      g.size += size * leaf_bytes;
      continue;
    }
    auto *target = a.extend(size * leaf_bytes);
    volume_wire_leaf_ranges(size, serial, [&](std::size_t first, std::size_t last) {
      for (auto i = first; i < last; ++i)
        std::memcpy(target + i * leaf_bytes, leaves.leaf(begin + i).buffer().data(), leaf_bytes);
    });
  }
  return true;
}
template <class Grid> openvdb::GridBase::Ptr read_volume_grid(Reader &a) {
  using Tree = typename Grid::TreeType;
  using Leaf = typename Tree::LeafNodeType;
  using Value = typename Grid::ValueType;
  std::string header;
  std::uint64_t count{}, per_chunk{};
  a(header, count, per_chunk);
  if (per_chunk < 1 || per_chunk > volume_wire_leaves_per_chunk<Grid>())
    throw std::invalid_argument("Invalid worker volume leaf chunk size");
  std::istringstream source(header, std::ios::binary);
  openvdb::io::setCurrentVersion(source);
  openvdb::io::setDataCompression(source, 0);
  auto grid = Grid::create();
  try {
    grid->readMeta(source);
    if (grid->saveFloatAsHalf())
      throw std::invalid_argument("half-float grid in the raw form");
    grid->readTransform(source);
    grid->tree().readTopology(source, false);
  } catch (const std::invalid_argument &) {
    throw;
  } catch (const std::exception &error) {
    throw std::invalid_argument(std::string("Invalid worker volume grid header: ") + error.what());
  }
  if (!source || source.tellg() != std::streampos(std::streamoff(header.size())))
    throw std::invalid_argument("Invalid worker volume grid header length");
  const bool serial = volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION");
  openvdb::tree::LeafManager<Tree> leaves(grid->tree(), 0, serial);
  if (count != leaves.leafCount())
    throw std::invalid_argument("Worker volume leaf count differs from its topology");
  constexpr std::size_t leaf_bytes = Leaf::SIZE * sizeof(Value);
  for (std::size_t begin = 0; begin < count; begin += per_chunk) {
    const auto size = std::size_t(std::min<std::uint64_t>(per_chunk, count - begin));
    const auto values = a.count();
    if (values != size * Leaf::SIZE || values > a.remaining() / sizeof(Value))
      throw std::invalid_argument("Worker volume leaf chunk exceeds packet bounds");
    a.allocate(std::uint64_t(size) * leaf_bytes);
    const auto *data = a.bytes.data() + a.position;
    volume_wire_leaf_ranges(size, serial, [&](std::size_t first, std::size_t last) {
      for (auto i = first; i < last; ++i)
        std::memcpy(leaves.leaf(begin + i).buffer().data(), data + i * leaf_bytes, leaf_bytes);
    });
    a.position += size * leaf_bytes;
  }
  return grid;
}
template <class A, class T>
  requires std::is_same_v<std::remove_cv_t<T>, VolumeData>
void fields(A &a, T &v) {
  a(v.key, v.material, v.revision, v.step_size, v.object_space_density, v.recover_material, v.velocity_seconds,
    v.velocity_reference_time);
  if constexpr (A::reading) {
    static std::once_flag registered;
    std::call_once(registered, []() { openvdb::initialize(); });
    const auto count = a.count();
    v.fields.reserve(count);
    for (std::size_t i = 0; i < count; ++i) {
      VolumeField field;
      std::uint8_t tag{};
      a(field.name, field.revision, tag);
      if (tag == volume_wire_float)
        field.grid = read_volume_grid<openvdb::FloatGrid>(a);
      else if (tag == volume_wire_vector)
        field.grid = read_volume_grid<openvdb::Vec3SGrid>(a);
      else if (tag == volume_wire_stream) {
        // Tag 0 is an opaque OpenVDB stream, not a container of DTOs. Its
        // unchanged u64 byte length may exceed the generic container ceiling.
        // Check the complete extent and aggregate budget before allocating.
        std::uint64_t size{};
        a(size);
        if (size > a.remaining())
          throw std::invalid_argument("Worker volume stream exceeds packet bounds");
        a.allocate(size + 1);
        std::string blob(std::size_t(size), '\0');
        a.raw(blob.data(), blob.size());
        std::istringstream source(std::move(blob), std::ios::binary);
        openvdb::io::Stream stream(source, false);
        const auto grids = stream.getGrids();
        if (!grids || grids->size() != 1)
          throw std::invalid_argument("Worker volume field must contain exactly one owned grid");
        field.grid = (*grids)[0];
      } else
        throw std::invalid_argument("Invalid worker volume field form");
      v.fields.push_back(std::move(field));
    }
  } else {
    a(std::uint64_t(v.fields.size()));
    const bool stream_only = volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_STREAM");
    for (const auto &field : v.fields) {
      if (!field.grid)
        throw std::invalid_argument("Cannot transport a null volume grid");
      a(field.name, field.revision);
      if (!stream_only) {
        if (const auto grid = openvdb::gridConstPtrCast<openvdb::FloatGrid>(field.grid))
          if (write_volume_grid(a, *grid, volume_wire_float))
            continue;
        if (const auto grid = openvdb::gridConstPtrCast<openvdb::Vec3SGrid>(field.grid))
          if (write_volume_grid(a, *grid, volume_wire_vector))
            continue;
      }
      std::ostringstream target(std::ios::binary);
      openvdb::io::Stream stream(target);
      stream.write(openvdb::GridCPtrVec{field.grid});
      // view() avoids duplicating a possibly multi-gigabyte archive. Preflight
      // the complete opaque field (including tag/length) before packet growth.
      const auto blob = target.view();
      std::uint64_t limit = maximum_packet;
      if (const auto *text = std::getenv("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT"))
        limit = std::min<std::uint64_t>(limit, std::strtoull(text, nullptr, 10));
      const auto available = limit > a.size() ? limit - a.size() : 0;
      if (available < 9 || blob.size() > available - 9)
        throw std::invalid_argument(
            "Volume field '" + field.name +
            "' is too large to send to the renderer: OpenVDB stream exceeds the scene update limit");
      a(std::uint8_t(volume_wire_stream), std::uint64_t(blob.size()));
      a.raw(blob.data(), blob.size());
    }
  }
}
template <class A, class T>
  requires std::is_same_v<std::remove_cv_t<T>, FrameEvent>
void fields(A &a, T &v) {
  a(v.pixel_aspect, v.samples, v.generation, v.sequence, v.x, v.y, v.width, v.height, v.full_width, v.full_height,
    v.rendered_width, v.rendered_height, v.published_seconds, v.planes, v.requested_passes, v.denoiser,
    v.denoise_device_id, v.complete, v.color_epoch, v.output_metadata);
  if constexpr (A::reading) {
    std::string policy;
    a(policy);
    if (!a.color || a.color->policy().render_policy_id != policy)
      throw std::invalid_argument("Worker frame has a different render-color policy");
    v.color_configuration = a.color;
  } else {
    if (!v.color_configuration)
      throw std::invalid_argument("Worker frame lacks color ownership");
    a(v.color_configuration->policy().render_policy_id);
  }
  validate_frame_image(v);
}
template <std::size_t I = 0> void read_shader(Reader &a, ShaderValue &value, std::uint8_t tag) {
  if constexpr (I < std::variant_size_v<ShaderValue>) {
    if (tag == I) {
      std::variant_alternative_t<I, ShaderValue> entry;
      a(entry);
      value = std::move(entry);
    } else
      read_shader<I + 1>(a, value, tag);
  } else
    throw std::invalid_argument("Unknown shader value wire tag");
}
// Protocol 92 instance records have no mesh ledger effects. Reuse their
// explicit field lists for both sizing and writing; gathered updates with large
// referenced arrays retain the previous encoder and its borrowed-array lifetime;
// each record owns a precomputed range, independent of task scheduling.
struct InstanceWriter {
  static constexpr bool reading = false;
  std::byte *target{};
  std::size_t position{}, limit{std::size_t(maximum_packet)};
  bool large_payload{};
  void raw(const void *source, std::size_t count) {
    if (count > limit - position)
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    if (target && count)
      std::memcpy(target + position, source, count);
    position += count;
  }
  template <class T> void io(const T &value) {
    using U = std::remove_cv_t<T>;
    if constexpr (std::is_same_v<U, bool>) {
      const std::uint8_t flag = value ? 1 : 0;
      raw(&flag, 1);
    } else if constexpr (std::is_arithmetic_v<U>) {
      raw(&value, sizeof(value));
    } else if constexpr (std::is_same_v<U, std::string>) {
      io(std::uint64_t(value.size()));
      raw(value.data(), value.size());
    } else if constexpr (vector_type<U>::value) {
      io(std::uint64_t(value.size()));
      if constexpr (bulk<typename U::value_type>) {
        if (value.size() > (limit - position) / sizeof(typename U::value_type))
          throw std::invalid_argument("Worker packet exceeds allocation limit");
        const auto bytes = value.size() * sizeof(typename U::value_type);
        large_payload |= bytes >= gather_reference_bytes;
        raw(value.data(), bytes);
      } else
        for (const auto &entry : value)
          io(entry);
    } else if constexpr (std::is_same_v<U, Matrix>) {
      raw(value.data(), sizeof(Matrix));
    } else if constexpr (optional_type<U>::value) {
      io(bool(value));
      if (value)
        io(*value);
    } else
      fields(*this, value);
  }
  template <class... T> void operator()(const T &...values) { (io(values), ...); }
};

template <class T> void Writer::instances(const T &value) {
  constexpr std::size_t grain = 512;
  // Every pose needs at least 65 bytes, every attribute command at least 17.
  // Reject impossible counts before the O(records) offset table is allocated.
  constexpr std::size_t minimum = std::is_same_v<typename T::value_type, ObjectPoseData> ? 65 : 17;
  static_assert(sizeof(std::size_t) >= sizeof(std::uint64_t));
  if (value.size() > (maximum_packet - size()) / minimum ||
      value.size() >= std::numeric_limits<std::size_t>::max() / sizeof(std::size_t))
    throw std::invalid_argument("Worker packet exceeds allocation limit");
  std::vector<std::size_t> offsets(value.size() + 1);
  std::atomic_bool reference_fallback{};
  const bool serial = volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION");
  const auto ranges = [&](const auto &function) {
    if (serial)
      function(std::size_t(0), value.size());
    else
      tbb::parallel_for(tbb::blocked_range<std::size_t>(0, value.size(), grain),
                        [&](const tbb::blocked_range<std::size_t> &range) { function(range.begin(), range.end()); });
  };
  ranges([&](std::size_t first, std::size_t end) {
    for (auto i = first; i != end; ++i) {
      InstanceWriter count;
      count(value[i]);
      offsets[i + 1] = count.position;
      if (gather && count.large_payload)
        reference_fallback.store(true, std::memory_order_relaxed);
    }
  });
  if (reference_fallback.load(std::memory_order_relaxed)) {
    for (const auto &entry : value)
      io(entry);
    return;
  }
  const auto available = maximum_packet - size();
  for (std::size_t i = 0; i != value.size(); ++i) {
    if (offsets[i + 1] > available - offsets[i])
      throw std::invalid_argument("Worker packet exceeds allocation limit");
    offsets[i + 1] += offsets[i];
  }
  auto *target = stage(offsets.back());
  ranges([&](std::size_t first, std::size_t end) {
    for (auto i = first; i != end; ++i) {
      InstanceWriter writer{target + offsets[i], 0, offsets[i + 1] - offsets[i]};
      writer(value[i]);
      if (writer.position != writer.limit)
        throw std::logic_error("Instance wire size changed during encoding");
    }
  });
}

template <class T> void Writer::io(const T &value) {
  using U = std::remove_cv_t<T>;
  if constexpr (std::is_same_v<U, bool>) {
    const std::uint8_t flag = value ? 1 : 0;
    raw(&flag, 1);
  } else if constexpr (std::is_enum_v<U>) {
    io(std::int32_t(value));
  } else if constexpr (std::is_arithmetic_v<U>) {
    raw(&value, sizeof(value));
  } else if constexpr (std::is_same_v<U, std::string>) {
    io(std::uint64_t(value.size()));
    raw(value.data(), value.size());
  } else if constexpr (vector_type<U>::value) {
    io(std::uint64_t(value.size()));
    if constexpr (std::is_same_v<typename U::value_type, ObjectPoseData> ||
                  std::is_same_v<typename U::value_type, ObjectAttributeData>) {
      // Small updates retain the inexpensive serial loop. The switch keeps the
      // complete previous encoder available for same-binary comparisons.
      if (value.size() >= 2048 && !volume_wire_env("CYCLES_HOUDINI_WIRE_INSTANCE_SERIAL"))
        instances(value);
      else
        for (const auto &entry : value)
          io(entry);
    } else if constexpr (bulk<typename U::value_type>)
      reference(value.data(), value.size() * sizeof(typename U::value_type));
    else
      for (const auto &entry : value)
        io(entry);
  } else if constexpr (std::is_same_v<U, Matrix>) {
    raw(value.data(), sizeof(Matrix));
  } else if constexpr (array_type<U>::value) {
    for (const auto &entry : value)
      io(entry);
  } else if constexpr (map_type<U>::value) {
    io(std::uint64_t(value.size()));
    for (const auto &[key, entry] : value)
      (*this)(key, entry);
  } else if constexpr (optional_type<U>::value || pointer_type<U>::value) {
    io(bool(value));
    if (value)
      io(*value);
  } else if constexpr (std::is_same_v<U, ShaderValue>) {
    io(std::uint8_t(value.index()));
    std::visit([&](const auto &entry) { io(entry); }, value);
  } else
    fields(*this, value);
}
template <class T> void Reader::io(T &value) {
  using U = std::remove_cv_t<T>;
  if constexpr (std::is_same_v<U, bool>) {
    std::uint8_t flag{};
    raw(&flag, 1);
    if (flag > 1)
      throw std::invalid_argument("Invalid worker boolean");
    value = flag != 0;
  } else if constexpr (std::is_enum_v<U>) {
    std::int32_t flag{};
    io(flag);
    value = static_cast<U>(flag);
  } else if constexpr (std::is_arithmetic_v<U>) {
    raw(&value, sizeof(value));
  } else if constexpr (std::is_same_v<U, std::string>) {
    const auto n = count();
    allocate(std::uint64_t(n) + 1);
    value.resize(n);
    raw(value.data(), n);
  } else if constexpr (vector_type<U>::value) {
    const auto n = count();
    if constexpr (bulk<typename U::value_type>) {
      if (n > remaining() / sizeof(typename U::value_type))
        throw std::invalid_argument("Worker vector exceeds packet bounds");
      allocate(std::uint64_t(n) * sizeof(typename U::value_type));
      using E = typename U::value_type;
      static_assert(std::is_trivially_copyable_v<E>);
      const auto size = n * sizeof(E);
      value.resize(n);
      if (size >= parallel_decode_bytes && !volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION")) {
        parallel_copy(reinterpret_cast<std::byte *>(value.data()), bytes.data() + position, size);
        position += size;
      } else if (n)
        raw(value.data(), size);
    } else {
      allocate(std::uint64_t(n) * sizeof(typename U::value_type));
      value.resize(n);
      if (n)
        for (auto &entry : value)
          io(entry);
    }
  } else if constexpr (std::is_same_v<U, Matrix>) {
    raw(value.data(), sizeof(Matrix));
  } else if constexpr (array_type<U>::value) {
    for (auto &entry : value)
      io(entry);
  } else if constexpr (map_type<U>::value) {
    const auto n = count();
    allocate(std::uint64_t(n) * (sizeof(typename U::value_type) + 4 * sizeof(void *)));
    for (std::size_t i = 0; i < n; ++i) {
      typename U::key_type key;
      typename U::mapped_type entry;
      (*this)(key, entry);
      if (!value.emplace(std::move(key), std::move(entry)).second)
        throw std::invalid_argument("Duplicate worker map key");
    }
  } else if constexpr (optional_type<U>::value) {
    bool present{};
    io(present);
    if (present) {
      typename optional_type<U>::element entry;
      io(entry);
      value = std::move(entry);
    } else
      value.reset();
  } else if constexpr (pointer_type<U>::value) {
    bool present{};
    io(present);
    if (present) {
      allocate(sizeof(typename pointer_type<U>::element) + 32);
      auto entry = std::make_shared<typename pointer_type<U>::element>();
      io(*entry);
      value = std::move(entry);
    } else
      value.reset();
  } else if constexpr (std::is_same_v<U, ShaderValue>) {
    std::uint8_t tag{};
    io(tag);
    read_shader(*this, value, tag);
  } else
    fields(*this, value);
}
template <class T> std::vector<std::byte> encode(const T &value, std::uint32_t kind) {
  Writer a;
  a(wire_magic, wire_version, kind, value);
  return std::move(a.bytes);
}
template <class T>
T decode(std::span<const std::byte> bytes, std::uint32_t kind,
         const std::shared_ptr<const ColorConfiguration> &color = {}) {
  Reader a(bytes);
  a.color = color;
  std::uint32_t magic{}, version{}, packet{};
  a(magic, version, packet);
  if (magic != wire_magic || version != wire_version || packet != kind)
    throw std::invalid_argument("Unsupported worker packet version/type");
  T value;
  a(value);
  a.finish();
  return value;
}
} // namespace
std::vector<std::byte> encode_worker_output(const WorkerOutput &v) { return encode(v, 5); }
WorkerOutput decode_worker_output(std::span<const std::byte> v) { return decode<WorkerOutput>(v, 5); }
std::vector<std::byte> encode_worker_preview(const PreviewRequest &v) { return encode(v, 6); }
PreviewRequest decode_worker_preview(std::span<const std::byte> v) {
  if (v.size() > 12 + 8 + 256 + 1)
    throw std::invalid_argument("Preview request exceeds bounds");
  return decode<PreviewRequest>(v, 6);
}
std::vector<std::byte> encode_worker_configuration(const WorkerConfiguration &v) { return encode(v, 1); }
WorkerConfiguration decode_worker_configuration(std::span<const std::byte> v) {
  return decode<WorkerConfiguration>(v, 1);
}
std::vector<std::byte> encode_worker_update(const WorkerUpdate &v, const MeshPayloadSources *acknowledged,
                                            bool reuse_mesh_payloads) {
  Writer a;
  a.acknowledged = acknowledged;
  a.reuse_mesh_payloads = reuse_mesh_payloads;
  a(wire_magic, wire_version, std::uint32_t(2), v);
  return std::move(a.bytes);
}
WorkerUpdate decode_worker_update(std::span<const std::byte> v, const MeshPayloadSources *acknowledged,
                                  std::uint64_t allocation_limit) {
  if (!allocation_limit || allocation_limit > maximum_packet)
    throw std::invalid_argument("Invalid decoded worker allocation limit");
  Reader a(v);
  a.acknowledged = acknowledged;
  a.allocation_limit = allocation_limit;
  std::uint32_t magic{}, version{}, kind{};
  a(magic, version, kind);
  if (magic != wire_magic || version != wire_version || kind != 2)
    throw std::invalid_argument("Unsupported worker packet version/type");
  WorkerUpdate result;
  a(result);
  a.finish();
  return result;
}
struct GatheredUpdate::Impl {
  std::shared_ptr<const WorkerUpdate> owner;
  Gather gather;
};
GatheredUpdate::GatheredUpdate() = default;
GatheredUpdate::GatheredUpdate(std::shared_ptr<const Impl> impl) : impl_(std::move(impl)) {}
std::uint64_t GatheredUpdate::size() const noexcept { return impl_ ? impl_->gather.size : 0; }
void GatheredUpdate::write(std::byte *target) const {
  if (!impl_ || !impl_->gather.size)
    return;
  // Fixed 4 MiB destination windows: each window copies the parts of the
  // segments it covers, so the result does not depend on scheduling.
  const auto &g = impl_->gather;
  const auto *segments = g.segments.data();
  const auto *segments_end = segments + g.segments.size();
  const auto *staging = g.staging.data();
  const auto *blocks = g.blocks.data();
  const std::uint64_t total = g.size;
  constexpr std::uint64_t window = 4ull << 20;
  const auto windows = (total + window - 1) / window;
  const auto fill = [=](std::uint64_t w) {
    const auto lo = w * window, hi = std::min(total, lo + window);
    auto *s = std::upper_bound(segments, segments_end, lo,
                               [](std::uint64_t value, const GatherSegment &x) { return value < x.offset; });
    for (--s; s != segments_end && s->offset < hi; ++s) {
      const auto from = std::max(lo, s->offset), to = std::min(hi, s->offset + s->size);
      if (from >= to)
        continue;
      const auto relative = from - s->offset;
      if (s->kind == GatherSegment::staged)
        std::memcpy(target + from, staging + s->first + relative, std::size_t(to - from));
      else if (s->kind == GatherSegment::external)
        std::memcpy(target + from, s->source + relative, std::size_t(to - from));
      else
        for (auto r = relative, end = to - s->offset; r < end;) {
          const auto index = r / s->block, offset = r % s->block;
          const auto n = std::min(s->block - offset, end - r);
          std::memcpy(target + s->offset + r, blocks[s->first + index] + offset, std::size_t(n));
          r += n;
        }
    }
  };
  if (windows <= 1 || volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION"))
    for (std::uint64_t w = 0; w < windows; ++w)
      fill(w);
  else
    tbb::parallel_for(tbb::blocked_range<std::uint64_t>(0, windows, 1),
                      [&](const tbb::blocked_range<std::uint64_t> &range) {
                        for (auto w = range.begin(); w != range.end(); ++w)
                          fill(w);
                      });
}
GatheredUpdate encode_worker_update_gathered(std::shared_ptr<const WorkerUpdate> v,
                                             const MeshPayloadSources *acknowledged, bool reuse_mesh_payloads) {
  if (!v)
    throw std::invalid_argument("Missing worker update");
  auto impl = std::make_shared<GatheredUpdate::Impl>();
  impl->owner = std::move(v);
  Writer a;
  a.gather = &impl->gather;
  a.acknowledged = acknowledged;
  a.reuse_mesh_payloads = reuse_mesh_payloads;
  a(wire_magic, wire_version, std::uint32_t(2), *impl->owner);
  return GatheredUpdate(std::move(impl));
}
MeshPayloadSources mesh_payload_sources(const MeshPayloadSources &previous, const SceneUpdate &accepted) {
  auto result = previous;
  for (const auto &key : accepted.deleted_geometry)
    result.erase(key);
  std::set<std::string> keys;
  for (const auto &mesh : accepted.meshes) {
    if (!mesh || mesh->key.empty() || !keys.insert(mesh->key).second)
      throw std::invalid_argument("Invalid mesh payload ledger update");
    if (const auto old = previous.find(mesh->key); old != previous.end() && old->second &&
                                                   old->second->revision == mesh->revision &&
                                                   !same_mesh_wire_payload(*old->second, *mesh))
      throw std::invalid_argument("Mesh payload changed without a revision");
    result.insert_or_assign(mesh->key, mesh);
  }
  return result;
}
std::vector<std::byte> encode_worker_color(const OwnedColorPolicy &v) { return encode(v, 4); }
OwnedColorPolicy decode_worker_color(std::span<const std::byte> v) { return decode<OwnedColorPolicy>(v, 4); }
std::vector<std::byte> encode_worker_reply(const WorkerReply &v) { return encode(v, 3); }
WorkerReply decode_worker_reply(std::span<const std::byte> v, const std::shared_ptr<const ColorConfiguration> &c) {
  auto result = decode<WorkerReply>(v, 3, c);
  if (result.focus_is_frame && (!result.frame || result.focus_frame))
    throw std::invalid_argument("Invalid worker focus-frame alias");
  return result;
}
} // namespace cycles_houdini
