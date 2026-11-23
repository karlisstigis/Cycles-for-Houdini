// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <span>
#include <utility>
#include <vector>
#include "cycles_houdini/shader.h"
#include "cycles_houdini/texture.h"
#include "cycles_houdini/diagnostic.h"
#include "cycles_houdini/shared_array.h"

// Internal integration contract. No private Cycles or Houdini types cross this boundary.
namespace cycles_houdini {

// An invalid candidate was rejected before entering the scene transaction.
// The last accepted scene remains owned by this session. Color-epoch changes
// are a separate lifecycle operation and can already have stopped tracing.
class SceneUpdateRejected : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};

struct Float2 {
  float x{}, y{};
  bool operator==(const Float2 &) const = default;
};
struct Float3 {
  float x{}, y{}, z{};
  bool operator==(const Float3 &) const = default;
};
struct Triangle {
  int a{}, b{}, c{};
  bool operator==(const Triangle &) const = default;
};
using Matrix = std::array<float, 12>; // Three row-major rows; translation is column 4.
inline constexpr Matrix identity_matrix = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0};

struct DeviceDescriptor {
  std::string id;
  std::string name;
  std::string backend;
  bool display_device{};
  std::vector<std::string> denoisers; // Effective backends; CPU choices are explicit.
  std::string physical_id;            // Same NVIDIA PCI identity for CUDA and OptiX.
};
struct Capabilities {
  std::string engine_commit;
  std::vector<DeviceDescriptor> devices;
  bool meshes{true};
  bool point_spheres{true};
  bool gaussian_splats{true};
  bool linear_round_curves{true};
  bool interactive_display{true};
  bool native_ipr{};
  bool volumes{};
  bool osl{};
  bool image_iris{};
  bool bdpt_prototype{}; // Isolated experiment; never enabled by a normal build.
  bool path_guiding{};   // OpenPGL compiled in; the CPU device must also report support.
};
Capabilities capabilities();
// Renderer/harness process opt-in; never call from an artist UI/menu probe.
void enable_renderer_device_probes();
void initialize(const std::string &resource_root, const std::string &user_cache_root = "");

enum class Storage { Float32 };
struct PassDescriptor {
  std::string id;
  std::vector<std::string> channels;
  Storage storage{Storage::Float32};
  bool filtered{};
  bool color{};
  bool denoise_eligible{};
  bool cryptomatte{}; // RG/BA are ID/coverage pairs, never color or alpha.
  bool internal{};    // Denoiser dependencies stay available for display, not file output.
  bool operator==(const PassDescriptor &) const = default;
};
std::vector<PassDescriptor> pass_catalogue();
void validate_passes(const std::vector<std::string> &passes);
struct AovDefinition {
  std::string name;
  bool color{true}; // Color: RGBA in the render working space; Value: scalar data.
  bool operator==(const AovDefinition &) const = default;
};
void validate_aovs(const std::vector<AovDefinition> &aovs);
std::vector<PassDescriptor> aov_descriptors(const std::vector<AovDefinition> &aovs);
void validate_lightgroup_assignment(const std::string &name);
void validate_lightgroups(const std::vector<std::string> &groups);
void validate_pass_outputs(const std::vector<AovDefinition> &aovs, const std::vector<std::string> &groups);
std::vector<PassDescriptor> lightgroup_descriptors(const std::vector<std::string> &groups);
struct CryptomatteSettings {
  bool object{}, material{}, asset{};
  int levels{6}; // ID/coverage pairs; an odd count rounds up to a complete RGBA layer.
  bool operator==(const CryptomatteSettings &) const = default;
};
void validate_cryptomatte(const CryptomatteSettings &settings);
void validate_cryptomatte_name(const std::string &name);
std::vector<PassDescriptor> cryptomatte_descriptors(const CryptomatteSettings &settings);

struct DenoiseSettings {
  bool enabled{};
  std::string method{"auto"}; // auto, oidn, optix, dlss (interactive only)
  bool use_gpu{true};         // OIDN on the render GPU; false explicitly selects CPU.
  std::string quality{"high"}, prefilter{"accurate"};
  int start_sample{16};                // Progressive start; the final image is always denoised.
  float upscale_factor{1.0f};          // DLSS input-to-display ratio; display dimensions stay authored.
  std::string passes{"albedo_normal"}; // rgb, albedo, albedo_normal
  bool operator==(const DenoiseSettings &) const = default;
};

struct RenderSettings {
  int samples{64};
  bool adaptive_sampling{};
  float noise_threshold{0.01f};
  int min_samples{};
  double time_limit{}; // Path tracing only; zero means unlimited.
  std::string sampling_pattern{"tabulated_sobol"};
  int seed{};
  bool animate_seed{};
  double frame{1};
  int min_bounce{}, max_bounce{7}, diffuse_bounce{7}, glossy_bounce{7}, transmission_bounce{7}, volume_bounce{7};
  int transparent_min_bounce{}, transparent_max_bounce{7};
  bool light_tree{true};
  float light_threshold{}, clamp_direct{}, clamp_indirect{10}, filter_glossy{};
  bool resampled_light_sampling{};  // Selective RIS next-event estimation for supported lights.
  bool use_guiding{};               // CPU only; preserve the authored choice while a GPU is selected.
  bool deterministic_guiding{true}; // Advanced OpenPGL training policy.
  bool nested_dielectrics{}; // Priority-based nested dielectric media (engine patch 0074). Off renders native Cycles.
  float glossy_lobe_floor{}; // Experimental build only: first-hit glossy pick-probability floor (0 = native Cycles).
  bool caustics_reflective{true}, caustics_refractive{true};
  bool fast_gi{};
  std::string fast_gi_method{"replace"};
  int ao_bounces{1};
  float ao_factor{1}, ao_distance{10};
  bool volume_biased{};
  float volume_step_rate{1};
  int volume_max_steps{1024};
  float film_exposure{1}, filter_width{1}, pass_alpha_threshold{0.5f};
  float mist_start{}, mist_depth{100}, mist_falloff{1};
  std::string pixel_filter{"box"};
  bool sample_subset{};
  int subset_offset{}, subset_length{1};
  bool operator==(const RenderSettings &) const = default;
};

struct ColorPolicy {
  std::string working_space, default_display;
  std::string configuration_id, render_policy_id;
  std::string output_interop_id, output_interop_selection, builtin_configuration;
  std::vector<std::string> texture_spaces, output_spaces;
  std::map<std::string, std::vector<std::string>> views;
  std::map<std::string, std::string> default_views;
};
ColorPolicy color_policy();
class ColorConfiguration {
public:
  ~ColorConfiguration();
  const ColorPolicy &policy() const;

private:
  struct Impl;
  explicit ColorConfiguration(std::shared_ptr<const Impl>);
  std::shared_ptr<const Impl> impl_;
  friend class ColorConfigurationAccess;
};
// Observe Houdini's authoring policy without interrupting independent workers.
bool synchronize_color_configuration();
struct DisplaySettings {
  std::string pass{"combined"}, display, view, channels{"rgba"};
  float exposure{}, gamma{1};
  bool raw{};
  // Viewer backgrounds as displayed 8-bit RGB (one solid colour, or the two
  // checker colours). For an RGBA colour view each one adds an opaque
  // composite View(2^exposure * RGB_premult + (1 - alpha) * B_linear) to
  // DisplayImage::composites, where View(B_linear) shows the background bytes.
  std::vector<std::array<std::uint8_t, 3>> backgrounds;
};
struct FrameEvent;
struct DisplayImage {
  std::uint64_t generation{}, sequence{};
  int x{}, y{}, width{}, height{}, full_width{}, full_height{};
  std::vector<std::uint8_t> rgba;
  // One opaque RGBA image per DisplaySettings::backgrounds entry, or empty.
  std::vector<std::vector<std::uint8_t>> composites;
};
struct DisplayTimings {
  double prepare_seconds{}, processor_seconds{}, transform_seconds{}, pack_seconds{}, total_seconds{};
};
DisplayImage display_image(const FrameEvent &frame, const DisplaySettings &settings, DisplayTimings *timings = nullptr);
struct DisplayGpuTexture {
  std::string sampler;
  int width{}, height{}, channels{};
  bool linear{};
  std::vector<float> values;
};
struct DisplayGpuTransform {
  std::string key, shader;
  std::vector<DisplayGpuTexture> textures;
};
// Null selects the exact CPU route for unsupported passes, dynamic uniforms,
// 3D LUTs, or an old frame requiring conversion across working policies.
std::shared_ptr<const DisplayGpuTransform> display_gpu_transform(const FrameEvent &, const DisplaySettings &);
// Scene-linear working-space value of each viewer background: the inverse of
// the same view/display (and display gamma), or, for a view without an
// inverse, the inverse of the display colour space. Empty when composites do
// not apply (no backgrounds, raw, data, alpha or RGB-only views, no alpha
// channel, or a frame from another rendering policy).
std::vector<std::array<float, 3>> display_backgrounds_linear(const FrameEvent &, const DisplaySettings &,
                                                             std::string *method = nullptr);

enum class MaterialKind { Diffuse, Principled, Emission, VolumeAbsorption, PrincipledVolume, Graph, DiagnosticSurface };
struct MaterialData {
  std::string key;
  MaterialKind kind{MaterialKind::Diffuse};
  Float3 color{1, 1, 1};
  float roughness{0.5f};
  float metallic{};
  float emission_strength{1};
  float density{1};
  float anisotropy{};
  float volume_emission_strength{};
  Float3 volume_emission_color{1, 1, 1};
  std::string emission_field; // Empty: emission follows density; otherwise a named scalar/RGB grid.
  float blackbody_intensity{};
  float temperature_kelvin_scale{1000}; // Explicit multiplier for the named temperature field.
  std::string color_attribute;          // Optional linear RGB/scalar multiplier, e.g. Cd.
  std::optional<ShaderGraphData> graph; // Owned surface/volume graph, schema/version checked.
  std::string cryptomatte_name;         // Empty uses the internal key; host authoring supplies a stable path.
  int pass_index{};                     // Explicit unfiltered Material Index, 0..32767.
  std::vector<SourceDiagnostic> source_diagnostics;
  // Frozen chronological displacement graphs over the same uniform shutter
  // interval as geometry. Surface shading/identity remains on the main graph.
  std::vector<ShaderGraphData> displacement_samples;
  bool operator==(const MaterialData &) const = default;
};
struct WorldData {
  // A missing graph uses SceneUpdate::background. World shaders share the
  // owned graph format but have a separate validated output domain.
  std::optional<ShaderGraphData> graph;
  std::uint32_t visibility{255};
  bool use_mis{true}, cast_shadow{true}, use_caustics{};
  int map_resolution{}, max_bounces{1024}; // Zero resolution: Cycles automatic.
  // World volume sampling: 0 distance, 1 equiangular, 2 multiple importance
  // (ccl::VolumeSampling). Equiangular is Blender's world default.
  int volume_sampling{1};
  std::string source_path; // Diagnostic context only; never evaluated by the engine.
  bool operator==(const WorldData &) const = default;
};
struct GeometryMotion {
  // Uniform time samples across the shutter, in chronological order with the
  // centre omitted. Base positions/radii are the centre. 2 other samples make
  // 3 total steps; 4 make 5, up to the pinned engine's 129-step limit.
  SharedArray<std::vector<Float3>> positions;
  SharedArray<std::vector<float>> radii; // Optional for points/curves only.
  bool operator==(const GeometryMotion &) const = default;
};

// Numeric Houdini shader fields. Values are owned and follow the component's
// shading domain: mesh corners, point spheres, or curve keys. Unused tuple
// components are zero; integers are converted to the renderer's float precision.
struct NamedAttribute {
  std::string name;
  int components{3};
  SharedArray<Float3> values;
  SharedArray<std::vector<Float3>> motion_values; // Non-centre shutter samples.
  bool motion_complete{true};                     // A source failure retains the centre field.
  bool operator==(const NamedAttribute &) const = default;
};
// One owned numeric value per render object. Packed copies can vary shading
// without duplicating their prototype's vertex/corner buffers.
struct UniformAttribute {
  std::string name;
  int components{1};
  Float3 value{}; // Requested-frame value, also used for local shutter recovery.
  // All chronological uniform shutter samples, including the shutter midpoint.
  // The requested frame can differ from every sample when the shutter is offset.
  // Empty means static; otherwise odd 3..129, independent of prototype motion.
  SharedArray<Float3> motion_values;
  bool motion_complete{true}; // False holds value; the host reports the source failure.
  bool operator==(const UniformAttribute &other) const {
    const auto equal = [](const Float3 &a, const Float3 &b) {
      return float_bits_equal(a.x, b.x) && float_bits_equal(a.y, b.y) && float_bits_equal(a.z, b.z);
    };
    if (name != other.name || components != other.components || !equal(value, other.value) ||
        motion_complete != other.motion_complete || motion_values.size() != other.motion_values.size())
      return false;
    if (motion_values.storage_identity() != other.motion_values.storage_identity())
      for (std::size_t i = 0; i < motion_values.size(); ++i)
        if (!equal(motion_values[i], other.motion_values[i]))
          return false;
    return true;
  }
};
using AttributeLayout = std::map<std::string, int>;
inline AttributeLayout attribute_layout(const std::vector<NamedAttribute> &attributes,
                                        const AttributeLayout &instances = {}) {
  auto result = instances;
  for (const auto &attribute : attributes)
    result.insert_or_assign(attribute.name, attribute.components);
  return result;
}
inline AttributeLayout attribute_layout(const std::vector<UniformAttribute> &attributes) {
  AttributeLayout result;
  for (const auto &attribute : attributes)
    result.emplace(attribute.name, attribute.components);
  return result;
}
enum class SubdivisionType { None, Linear, CatmullClark };
enum class SubdivisionSpace { Object, Pixel };
enum class SubdivisionBoundary { None, EdgeOnly, EdgeAndCorner };
enum class SubdivisionFVar { None, CornersOnly, CornersPlus1, CornersPlus2, Boundaries, All };
struct SubdivisionSettings {
  SubdivisionType type{SubdivisionType::None};
  SubdivisionSpace space{SubdivisionSpace::Pixel};
  SubdivisionBoundary boundary{SubdivisionBoundary::EdgeAndCorner};
  SubdivisionFVar fvar{SubdivisionFVar::Boundaries};
  float dicing_rate{2}; // Pixel lengths, or object-space lengths in Object mode.
  int max_level{6};
  std::uint64_t triangle_limit{}; // Optional per-mesh allocation guard; zero disables it.
  bool adaptive{true};            // False samples the limit surface at uniform subdivision levels.
  bool use_creases{true};
  bool operator==(const SubdivisionSettings &) const = default;
};
struct EdgeCrease {
  int a{}, b{};
  float sharpness{};
  bool operator==(const EdgeCrease &) const = default;
}; // OpenSubdiv units [0,10], not normalized Cycles weights.
struct VertexCrease {
  int point{};
  float sharpness{};
  bool operator==(const VertexCrease &) const = default;
};
struct SubdivisionData {
  SubdivisionSettings settings;
  SharedArray<int> face_sizes, face_corners;
  std::vector<int> face_material;
  // Original Houdini primitive index and polygon-within-soup. Owned provenance
  // distinguishes soup faces; it is not a stable identity across SOP recooks.
  SharedArray<std::array<std::int64_t, 2>> face_provenance;
  SharedArray<EdgeCrease> edge_creases;
  SharedArray<VertexCrease> vertex_creases;
  bool operator==(const SubdivisionData &) const = default;
};
struct MeshNominalPose {
  // Optional requested-frame data, aligned to the shutter-centre topology.
  // Only offset shutters need this owned recovery pose.
  SharedArray<Float3> positions, corner_normals;
  SharedArray<Float2> corner_uv;
  SharedArray<Float3> corner_colors;
  std::vector<NamedAttribute> attributes;
  bool operator==(const MeshNominalPose &) const = default;
};
struct CurveData;
struct PointData;
struct MeshData {
  std::string key;
  std::uint64_t revision{};
  SharedArray<Float3> positions;
  SharedArray<Triangle> triangles;
  // Corner arrays address coarse face corners when subdivision is present,
  // otherwise three corners per render triangle. Catmull-Clark uses limit normals.
  SharedArray<Float3> corner_normals;
  SharedArray<Float2> corner_uv;
  SharedArray<Float3> corner_colors; // Optional linear Cd.
  // Optional intrinsic values computed from original polygons before render
  // triangulation/seams. Empty low-level meshes derive them from their faces.
  SharedArray<float> pointiness;
  std::vector<std::string> materials;
  std::vector<int> triangle_material; // Optional; one slot per triangle.
  bool smooth{};
  GeometryMotion motion;
  SharedArray<std::vector<Float3>> motion_corner_normals;
  // Same centre-first storage convention as geometry motion: these arrays
  // omit the centre, and empty arrays mean constant attributes.
  SharedArray<std::vector<Float2>> motion_corner_uv;
  SharedArray<std::vector<Float3>> motion_corner_colors;
  bool motion_uv_complete{true}, motion_color_complete{true};
  std::optional<SubdivisionData> subdivision; // Original polygons; triangles must be empty.
  std::optional<MeshNominalPose> nominal_pose;
  // Optional Blender texture space {location, size} for Generated coordinates (Link meshes,
  // core/generated_coordinates.h); empty: the undeformed bounds of the positions.
  SharedArray<Float3> texture_space;
  // Optional Blender topology of a Link mesh for Pointiness and Random per Island, as Blender's
  // translator uses it (v5.3.0 intern/cycles/blender/mesh.cpp, attr_create_pointiness and
  // attr_create_random_per_island; core/mesh_pointiness.h, core/mesh_islands.h): the mesh's
  // edges() as vertex pairs in Blender's edge order (polygon and loose edges), and Blender's
  // vertex normals of the requested-frame pose. Non-empty vertex normals select Blender's rules;
  // empty: both attributes derive from the faces (Houdini meshes).
  SharedArray<int> blender_edges;
  SharedArray<Float3> blender_vertex_normals;
  bool motion_blur{true}; // Native object opt-out also applies to generated displacement motion.
  std::vector<NamedAttribute> attributes;
  AttributeLayout instance_attributes; // Required object fallbacks, never geometry buffers.
  // Host-side carrier of Link (Blender) hair through the external-geometry path: when set, the
  // mesh arrays above are empty and the scene adapter publishes these curves under this
  // record's key instead of a mesh (houdini/blend_mesh.cpp capture_blend_curves). Never part of
  // the engine's mesh payload, wire or snapshot.
  std::shared_ptr<const CurveData> external_curves;
  // The same carrier for Link (Blender) point clouds (capture_blend_points): the scene adapter
  // publishes these points under this record's key. Host-only like external_curves.
  std::shared_ptr<const PointData> external_points;
};
// Owned Gaussian parameters. Quaternions use Houdini's xyzw order; radiance
// stores the unscaled sRGB SH DC coefficient and opacity (Cycles convention).
struct GaussianSplatData {
  bool scene_linear_color{}; // Without SH, radiance RGB can own working-space Cd.
  std::vector<Float3> scales;
  std::vector<std::array<float, 4>> rotations, radiance;
  std::vector<std::array<Float3, 15>> harmonics;
  std::vector<std::vector<Float3>> motion_scales;
  std::vector<std::vector<std::array<float, 4>>> motion_rotations, motion_radiance;
  bool operator==(const GaussianSplatData &) const = default;
};
struct PointData {
  std::string key;
  std::uint64_t revision{};
  std::string material;
  // An explicit palette replaces the legacy uniform material. Slots index
  // that palette once per point; omitted slots select its first entry.
  std::vector<std::string> materials;
  std::vector<int> material_slots;
  std::vector<Float3> positions;
  std::vector<float> radii;
  std::vector<Float3> colors; // Optional point Cd, in the render working space.
  std::vector<float> randoms; // Optional stable normalized point identity hashes.
  std::vector<Float2> uv;
  // Optional per-point [birth, death) interval in normalized shutter time.
  // Equal endpoints mean no existence; missing means alive throughout.
  // Explicit low-level engine data only; native SOP attributes never populate it.
  std::vector<Float2> lifetimes;
  std::optional<GaussianSplatData> splats;
  GeometryMotion motion;
  // Generated texture coordinates (core/session_strands.cpp). Default: the point in the bounds
  // of all points. Link point clouds follow Blender's translator (v5.3.0
  // intern/cycles/blender/pointcloud.cpp export_pointcloud adds no Generated attribute), so
  // Cycles falls back to the object-space shading position (generated_attribute false).
  bool generated_attribute{true};
  std::vector<NamedAttribute> attributes;
  std::vector<std::vector<Float3>> motion_colors;
  std::vector<std::vector<Float2>> motion_uv;
  bool motion_color_complete{true}, motion_uv_complete{true};
  AttributeLayout instance_attributes;
};
enum class CurveShape { Linear, Thick, Ribbon };
struct CurveData {
  std::string key;
  std::uint64_t revision{};
  std::string material;
  // One shader slot per complete curve, rather than per curve key.
  std::vector<std::string> materials;
  std::vector<int> material_slots;
  std::vector<Float3> positions;
  std::vector<float> radii;
  std::vector<int> first_key; // Each open linear curve has at least two keys.
  std::vector<Float3> colors; // Optional per-key Cd.
  std::vector<float> randoms; // Optional stable per-curve identity hashes.
  std::vector<Float2> uv;     // Optional root UV, one value per curve.
  GeometryMotion motion;
  CurveShape shape{CurveShape::Linear};
  // Generated texture coordinates (core/session_strands.cpp). Default: the curve root in the
  // bounds of all keys. Link curves follow Blender's translator (v5.3.0
  // intern/cycles/blender/curves.cpp): legacy particle hair gives one explicit value per curve
  // (root key in the emitter's texture space); a Curves object has no Generated attribute, so
  // Cycles falls back to the object-space shading position (generated_attribute false).
  std::vector<Float3> generated;
  bool generated_attribute{true};
  std::vector<NamedAttribute> attributes;
  std::vector<std::vector<Float3>> motion_colors;
  std::vector<std::vector<Float2>> motion_uv;
  bool motion_color_complete{true}, motion_uv_complete{true};
  AttributeLayout instance_attributes;
};
struct ObjectProperties {
  bool holdout{}, caustics_caster{}, caustics_receiver{};
  float shadow_terminator_shading{}, shadow_terminator_geometry{0.1f}, ao_distance{};
  std::string lightgroup; // Emission from every component of this OBJ/packed instance.
  std::string cryptomatte_object, cryptomatte_asset;
  int pass_index{}; // Inherited by every component and packed child of this OBJ.
  bool shadow_catcher{};
  std::string linking_id; // Host OBJ/root identity; shared by its packed children.
  // Nested dielectrics: higher wins, 0 is an ordinary value. A packed instance field
  // named dielectric_priority_attribute overrides it per instance.
  int dielectric_priority{};
  // Object Info Color/Alpha. Cycles' defaults (0) for Houdini objects; Link members carry
  // Blender's object colour (BlenderObjectValues, docs/dev/blend-import.md).
  Float3 color{};
  float alpha{};
  bool operator==(const ObjectProperties &) const = default;
};
void validate_object_properties(const ObjectProperties &properties);
void validate_pass_index(int index);
// Priorities travel through float instance fields; keep them exactly representable.
inline constexpr int dielectric_priority_limit = 16777216;
inline constexpr const char *dielectric_priority_attribute = "cycles_dielectric_priority";
void validate_dielectric_priority(int priority);
struct ObjectData {
  std::string key;
  std::string geometry_key;
  Matrix transform{identity_matrix};
  std::uint32_t visibility{0xffffffffu};
  std::uint32_t random_id{};
  std::vector<Matrix> motion; // Uniform chronological transforms, centre included.
  ObjectProperties properties;
  std::optional<Matrix> nominal_transform; // Requested-frame transform for offset-shutter recovery.
  std::vector<UniformAttribute> attributes;
  bool operator==(const ObjectData &) const = default;
};
// An existing resource can change pose without repeating its geometry binding,
// visibility, names and shading properties. These remain owned by ObjectData.
struct ObjectPoseData {
  std::string key;
  Matrix transform{identity_matrix};
  std::vector<Matrix> motion;
  std::optional<Matrix> nominal_transform;
  bool operator==(const ObjectPoseData &) const = default;
};
// Complete replacement of the current object's uniform fields. Empty clears
// fields. An optional complete authored pose replaces transform/motion/nominal
// state atomically; its key must exactly equal this command's nonempty key.
// All binding and shading properties remain owned by the accepted object.
struct ObjectAttributeData {
  std::string key;
  std::vector<UniformAttribute> attributes;
  std::optional<ObjectPoseData> pose{};
  bool operator==(const ObjectAttributeData &) const = default;
};
enum class ObjectUpdateKind { Unchanged, Pose, Attributes, PoseAttributes, Full };
inline ObjectUpdateKind classify_object_update(const ObjectData &before, const ObjectData &after) {
  if (before == after)
    return ObjectUpdateKind::Unchanged;
  if (before.key != after.key || before.geometry_key != after.geometry_key || before.visibility != after.visibility ||
      before.random_id != after.random_id || before.properties != after.properties)
    return ObjectUpdateKind::Full;
  if (before.attributes == after.attributes)
    return ObjectUpdateKind::Pose;
  if (before.transform == after.transform && before.motion == after.motion &&
      before.nominal_transform == after.nominal_transform)
    return ObjectUpdateKind::Attributes;
  return ObjectUpdateKind::PoseAttributes;
}
enum class LightKind { Point, Area, Sun, Spot };
struct LightCone {
  float inner_half_angle{}, outer_half_angle{}, rolloff{1};
  bool reverse_axis{};
  bool operator==(const LightCone &) const = default;
};
struct LightData {
  std::string key;
  LightKind kind{LightKind::Point};
  Matrix transform{identity_matrix};
  Float3 strength{100, 100, 100}; // Engine radiometric convention, not Houdini intensity.
  float radius{0.05f};
  float size_u{1}, size_v{1};
  float angle{0.00918f};
  bool normalize{true}, cast_shadow{true}, use_mis{};
  std::uint32_t visibility{0xffffffffu};
  bool ellipse{}, is_sphere{true}, use_caustics{};
  float spread{3.141592654f}, spot_smooth{};
  int max_bounces{1024};
  bool is_portal{}; // Area only: guides world sampling through its shape and emits nothing.
  std::optional<LightCone> cone;
  std::string lightgroup;
  std::string cryptomatte_object, cryptomatte_asset;
  bool shadow_catcher{true};            // Lighting in the real/reference scene used for catcher division.
  std::optional<ShaderGraphData> graph; // Emission multiplier; native power/color remain independent.
  std::string source_path;              // Owned diagnostic context.
  std::string linking_id;               // Empty uses key; both sides of a native area share one ID.
  bool operator==(const LightData &) const = default;
};
struct CameraSampling {
  bool rolling_shutter{};           // Pinned engine supports top-to-bottom scanning.
  float rolling_duration{0.1f};     // 0: scan only; 1: global shutter.
  std::vector<float> shutter_curve; // Empty is uniform; nonnegative relative weights.
  bool operator==(const CameraSampling &) const = default;
};
struct PanoramaSettings {
  std::string type{"equirectangular"};
  float fisheye_fov{3.14159265359f}, fisheye_lens{10.5f}; // Radians; millimetres.
  float latitude_min{-1.57079632679f}, latitude_max{1.57079632679f};
  float longitude_min{-3.14159265359f}, longitude_max{3.14159265359f};
  std::array<float, 5> polynomial{-1.17351e-5f, -0.0199887f, -3.3525e-6f, 3.0993e-6f, -2.61e-8f};
  float cylindrical_u_min{-3.14159265359f}, cylindrical_u_max{3.14159265359f};
  float cylindrical_v_min{-1}, cylindrical_v_max{1}; // Height/radius ratios.
  float sensor_width{36}, sensor_height{24};         // Millimetres, before sensor fitting.
  std::string sensor_fit{"auto"};
  bool operator==(const PanoramaSettings &) const = default;
};
struct CameraDofSample {
  float aperture_size{}, focus_distance{10}, aperture_rotation{}, aperture_ratio{1};
  bool operator==(const CameraDofSample &) const = default;
};
struct CameraIntrinsicSample {
  float short_axis_fov{0.785398163f}, near_clip{.01f}, far_clip{10000}, ortho_scale{2};
  std::optional<PanoramaSettings> panorama;
  bool operator==(const CameraIntrinsicSample &) const = default;
};
struct CameraData {
  Matrix transform{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -1, 5}; // Requested pose; Cycles looks along +Z.
  float short_axis_fov{0.785398163f};                    // Cycles field of view on the shorter image axis, radians.
  float near_clip{0.01f}, far_clip{10000};
  bool orthographic{};
  float ortho_scale{2};
  float aperture_size{}, focus_distance{10}, aperture_rotation{}, aperture_ratio{1};
  int aperture_blades{};
  // Optional aperture image, held at the requested shutter frame.
  // Method 0: analytic lens, 1: marginal/conditional CDF, 2: hierarchy.
  std::string image_iris;
  int image_iris_method{};
  bool image_iris_color{}; // Exposure-compensated RGB transmission, optional.
  // Requested-frame optics remain above. Complete, owned chronological
  // shutter samples (odd 3..129) vary only the continuous DOF controls.
  // Blade count is discrete and stays at the requested frame.
  std::vector<CameraDofSample> dof_motion;
  // Nominal projection remains above; these complete chronological samples
  // contain only the active, locally recovered continuous projection fields.
  std::vector<CameraIntrinsicSample> intrinsic_motion;
  float shutter_frames{0.5f}; // Frames; host sampling converts its interval to seconds.
  // Complete chronological camera poses. A shifted shutter's midpoint may
  // differ from transform; ray generation interpolates this full array.
  std::vector<Matrix> motion;
  float pixel_aspect{1};
  CameraSampling sampling;
  std::optional<PanoramaSettings> panorama;
  std::vector<SourceDiagnostic> source_diagnostics; // Owned camera-domain warnings, replaced with this camera.
  bool operator==(const CameraData &) const = default;
};
struct Crop {
  int x{}, y{}, width{}, height{};
}; // Bottom-left pixel coordinates.
struct DicingSettings {
  float offscreen_scale{4};
  std::optional<CameraData> camera; // Empty follows the render camera.
  bool update{true};                // Automatic adaptive tessellation updates in IPR.
  std::uint64_t refresh_revision{}; // A changed revision requests one explicit update.
  bool operator==(const DicingSettings &) const = default;
};
struct SessionConfig {
  std::string device_id;               // Required: never fall back silently.
  std::vector<std::string> device_ids; // Alternative explicit selection; one backend, no CPU/GPU mixing.
  int width{640}, height{360}, samples{64}, threads{};
  bool interactive{true};
  bool progressive_display{}; // Final-render previews without IPR sampling or resolution policy.
  bool use_tiling{};          // Final rendering only; IPR always retains whole-frame buffers.
  int tile_size{2048};
  bool persistent_updates{};  // Control-worker experiment: mutate only between render iterations.
  bool collect_diagnostics{}; // Opt-in; benchmark snapshots, no stdout reporting.
  bool use_texture_cache{true}, auto_texture_cache{};
  // ROP Curve Subdivisions (Blender's scene.cycles_curves.subdivisions, default 2; Blender's
  // range 0-24). A Cycles SceneParams value: changing it recreates the session.
  int hair_subdivisions{2};
  // ROP Refit Meshes While Interacting: live renders refit the acceleration structure of a mesh whose
  // triangles are unchanged instead of rebuilding it. Ignored unless interactive.
  bool refit_meshes{};
  std::vector<std::string> passes{"combined"};
  std::vector<AovDefinition> aovs;
  std::vector<std::string> lightgroups;
  CryptomatteSettings cryptomatte;
  TextureInputPolicy texture_input_policy;
  DenoiseSettings denoising;
};
struct VolumeData;
struct LinkingRule {
  std::vector<std::string> objects, emitters;
  bool exclude{}; // False: only these emitters; true: every emitter except these.
  bool operator==(const LinkingRule &) const = default;
};
struct LinkingSettings {
  std::vector<LinkingRule> receivers, blockers;
  bool operator==(const LinkingSettings &) const = default;
};
struct SceneUpdate {
  std::optional<std::vector<SourceDiagnostic>> source_diagnostics;
  std::optional<std::vector<std::string>> passes;
  std::optional<std::vector<AovDefinition>> aovs;
  std::optional<std::vector<std::string>> lightgroups;
  std::optional<CryptomatteSettings> cryptomatte;
  std::optional<DenoiseSettings> denoising;
  std::optional<RenderSettings> rendering;
  std::vector<MaterialData> materials;
  std::vector<std::shared_ptr<const MeshData>> meshes;
  std::vector<std::shared_ptr<const VolumeData>> volumes;
  std::vector<std::shared_ptr<const PointData>> points;
  std::vector<std::shared_ptr<const CurveData>> curves;
  std::vector<ObjectData> objects;
  std::vector<ObjectPoseData> object_poses;
  std::vector<ObjectAttributeData> object_attributes;
  std::vector<LightData> lights;
  std::vector<std::string> deleted_objects, deleted_geometry, deleted_materials, deleted_lights;
  std::optional<CameraData> camera;
  std::optional<DicingSettings> dicing;
  std::optional<Crop> crop;
  std::optional<Float3> background;
  std::optional<WorldData> world;
  std::optional<LinkingSettings> linking;
  std::optional<std::string> background_lightgroup;
  std::optional<bool> transparent_background, transparent_glass;
  std::optional<float> transparent_roughness_threshold;
  std::optional<bool> motion_blur;
  // Interactive updates can move the render camera while retaining completed
  // pixel-adaptive tessellation. A later false update refines the latest view.
  bool defer_camera_subdivision{};
  std::optional<bool> background_shadow_catcher;
};

enum class SessionState { Idle, Rendering, Paused, Complete, Cancelled, Failed, Closed };
struct ImagePlane {
  std::string pass;
  int channels{};
  std::vector<float> pixels;
  // Immutable packed storage, independent of the frame's logical coverage.
  // Both zero means full coverage (including authored/reference frames).
  // Noisy/data and denoised passes may have different effective dimensions.
  int width{}, height{};
};
struct FrameEvent {
  float pixel_aspect{1};
  int samples{}; // Output tile sample budget actually consumed, before per-pixel convergence.
  std::uint64_t generation{}, sequence{};
  int x{}, y{}, width{}, height{}, full_width{}, full_height{};
  int rendered_width{}, rendered_height{}; // Effective resolution before display enlargement.
  double published_seconds{};              // Wall time since this generation's core update began.
  std::vector<ImagePlane> planes;
  std::vector<PassDescriptor> requested_passes;
  // Full manifests are immutable and accompany completed output only. Large
  // manifests are not copied or sent over the worker pipe on progressive tiles.
  std::shared_ptr<const std::map<std::string, std::string>> output_metadata;
  std::string denoiser{"none"}, denoise_device_id;
  bool complete{};
  std::shared_ptr<const ColorConfiguration> color_configuration;
  std::uint64_t color_epoch{};
};
std::pair<int, int> image_plane_size(const FrameEvent &, const ImagePlane &);
void validate_frame_image(const FrameEvent &);
void copy_image_plane_rows(const FrameEvent &, const ImagePlane &, int first_row, int rows, float *target);
std::vector<float> expand_image_plane(const FrameEvent &, const ImagePlane &);
struct FocusSample {
  Float3 position;
  float depth;
};
// A single logical pixel, sampled directly from immutable compact storage.
// No full-image expansion, filtering or display/color conversion is involved.
FocusSample sample_focus(const FrameEvent &, int x, int y);
struct PreviewRequest {
  std::string pass{"combined"};
  bool focus{};
};
// Display pixels are top-down, whereas engine storage is bottom-up. Enlarge
// only quantized RGBA with the same nearest/floor placement as native floats.
std::vector<std::uint8_t> expand_display_pixels(std::span<const std::uint8_t>, int source_width, int source_height,
                                                int width, int height);
struct SessionStatus {
  std::vector<SourceDiagnostic> source_diagnostics;
  int current_sample{}; // Engine progress, per current render tile.
  SessionState state{SessionState::Idle};
  std::uint64_t generation{}, geometry_conversions{}, object_updates{}, unchanged_reuses{};
  std::uint64_t direct_attribute_updates{};   // Last accepted apply; rejected candidates do not replace it.
  std::uint64_t expanded_attribute_updates{}; // Field commands requiring complete object preparation.
  std::uint64_t attribute_fallback_count{};   // Accepted physical applies with at least one expanded field command.
  std::string attribute_fallback_reason;      // Bounded reason for the last accepted apply; empty on a no-op.
  std::uint64_t image_iris_preparations{}, image_iris_table_bytes{};
  double image_iris_preparation_seconds{};
  RenderSettings rendering;
  std::string message, error;
  std::vector<std::string> render_device_ids; // Validated devices actually instantiated by this session.
  double progress{}, elapsed_seconds{}, first_image_seconds{}, last_update_seconds{};
  std::size_t owned_geometry_bytes{}, device_allocation_bytes{}, device_allocation_peak_bytes{};
  std::size_t geometry_resources{}, object_resources{}, material_resources{}, light_resources{};
  std::size_t engine_shader_resources{}, retired_shader_resources{};
  std::uint64_t shader_reuses{};
  std::size_t mesh_variant_resources{}, mesh_variant_bytes{}, retained_mesh_source_bytes{};
  // Unique family-owned metadata, counted once across logical aliases.
  std::size_t mesh_family_resources{}, mesh_family_members{}, mesh_variant_bindings{}, mesh_metric_records{};
  std::uint64_t displacement_rebuilds{};
  // Mesh updates tagged for an acceleration-structure refit (Refit Meshes While Interacting).
  std::uint64_t mesh_refits{};
  std::size_t subdivision_control_faces{}, subdivision_vertices{}, subdivision_triangles{};
  std::uint64_t subdivision_tessellations{};
  std::uint64_t volume_vector_merges{};                             // Cumulative demanded scalar-triplet assemblies.
  std::size_t volume_vector_fields{}, volume_vector_source_bytes{}; // Live derived CPU grids.
  bool volume_ray_marching{}; // Active session policy, including static volumes in a moving-volume scene.
  bool color_update_pending{};
  std::uint64_t color_epoch{}, color_target_epoch{};
  std::string denoiser{"none"}, denoise_device_id;
  // Nested dielectrics, read back from the engine scene. The per-object scan
  // runs only in diagnostic sessions (collect_diagnostics).
  bool engine_nested_dielectrics{};
  std::uint64_t dielectric_priority_objects{}; // Engine objects whose priority is not 0.
  int dielectric_priority_min{}, dielectric_priority_max{};
  // Engine render warnings (patch 0074 getters), also reported as render.* source diagnostics.
  std::uint64_t nested_probe_unresolved{}; // Media probes that ended unresolved in the current render.
  bool nested_stack_depth_clamped{};
  int nested_stack_depth{};
};
struct BufferCopyDiagnostic {
  std::uint64_t calls{};
  std::size_t requested_bytes{};
  double host_seconds{};
};
// Tracked device memory of the latest scene device update, per leaf render device (engine
// Scene::get_device_update_memory). Bytes of tracked device allocations, not physical residency or
// total VRAM usage; the aggregate lifetime peak (device_allocation_peak_bytes) is unchanged.
struct DeviceUpdateMemoryDevice {
  std::string id;          // Exact Cycles DeviceInfo id.
  std::string physical_id; // Same NVIDIA PCI identity for CUDA and OptiX (see DeviceDescriptor).
  std::string backend;     // Cycles device type name, e.g. OPTIX, CUDA, CPU.
  std::string description; // Not unique for two cards of one model.
  std::uint64_t start_bytes{}, peak_bytes{}, end_bytes{};
  std::uint64_t max_build_temp_bytes{}, max_build_temp_allocation_bytes{};
  std::uint64_t build_count{}, reused_temp_build_count{}, local_temp_build_count{};
  std::uint64_t kept_temp_bytes_before_attributes{};
};
struct DeviceUpdateMemory {
  bool available{};              // False until the session's engine has recorded an update.
  std::uint64_t update_serial{}; // Scene device updates of this engine session, from 1.
  std::uint64_t generation{};    // Session generation the update started for.
  std::string outcome;           // complete, cancelled or error.
  std::string scope;             // scene_device_update.
  std::vector<DeviceUpdateMemoryDevice> devices;
};
struct SessionDiagnostics {
  bool enabled{};
  std::uint64_t generation{}, display_updates{}, output_deliveries{};
  double core_update_seconds{}, cancel_join_seconds{}, display_conversion_seconds{}, output_delivery_seconds{};
  double scene_boundary_wait_seconds{};
  std::uint64_t session_cancel_calls{};
  double shader_graph_preparation_seconds{}; // Before core cancellation/update, included in host apply latency.
  double volume_vector_assembly_seconds{};   // Owned field preparation before the engine boundary.
  // Work completed by the old generation during the last cancellation join.
  // Callback timings can include work that began before cancellation; they
  // are not disjoint subintervals or measurements of physical GPU transfers.
  std::uint64_t cancel_output_deliveries{}, cancel_display_updates{};
  double cancel_output_delivery_seconds{}, cancel_display_conversion_seconds{};
  std::uint64_t output_reads_skipped{}, display_updates_skipped{};
  std::uint64_t cancel_output_reads_skipped{}, cancel_display_updates_skipped{};
  // Statistics are omitted while the render thread owns the scene lock.
  // Reading diagnostics must never wait for a BVH/displacement update.
  bool engine_statistics_pending{};
  double engine_snapshot_wait_seconds{};                                 // This call's nonblocking lock attempt.
  std::uint64_t material_builds{}, light_updates{}, background_builds{}; // Cumulative session counts.
  // Nested engine timers can overlap; they must not be summed as disjoint phases.
  std::map<std::string, double> engine_update_seconds;
  // High-level host-backed buffer copy requests; excludes constant memory,
  // device-only operations, backend transfers and physical GPU residency.
  std::map<std::string, BufferCopyDiagnostic> buffer_copy_requests;
  // Owned float32 output planes, not physical device transfer measurements.
  std::map<std::string, BufferCopyDiagnostic> output_plane_reads;
  // Latest scene device update's tracked memory per device; read without waiting for the scene lock.
  DeviceUpdateMemory device_update_memory;
};

class RenderSession {
public:
  explicit RenderSession(const SessionConfig &config);
  ~RenderSession();
  RenderSession(const RenderSession &) = delete;
  RenderSession &operator=(const RenderSession &) = delete;
  // A display attachment may retain a live/completed render only after the
  // complete transaction and external assets have been validated unchanged.
  // Explicit Render keeps the default, which starts a fresh generation.
  std::uint64_t apply(const SceneUpdate &update, bool reuse_if_unchanged = false);
  void pause(bool paused);
  // Isolated worker only: join this renderer before changing its process OCIO
  // config. Preserve pause/cancel and retain the old frame's source meaning.
  void prepare_color_configuration_change();
  void cancel();
  void close();
  SessionStatus status() const;
  SessionDiagnostics diagnostics() const;
  // Immutable source owners for worker transport. Derived/displaced engine
  // buffers are excluded, and caller-owned input buffers remain isolated.
  std::map<std::string, std::shared_ptr<const MeshData>> retained_mesh_sources() const;
  bool textures_changed() const;
  std::vector<TextureInputInfo> texture_inputs() const;
  std::shared_ptr<const FrameEvent> frame(std::uint64_t after_sequence = 0) const;
  std::vector<PassDescriptor> passes() const;
  // Viewer preference only. Does not cancel, synchronize geometry or reset samples.
  // "*" supplies every enabled pass for Houdini's native multi-plane viewer.
  void set_preview_pass(const std::string &pass);
  void set_preview(const PreviewRequest &request);
  // At most one current-revision Position/Depth buffer, refreshed as the
  // progressive resolution improves. Independent of displayed beauty tiles.
  std::shared_ptr<const FrameEvent> focus_frame(std::uint64_t after_sequence = 0) const;
  struct OutputPass {
    std::string source, layer;
    std::string precision{"inherit"};
    std::string part, compression; // Empty inherits the file's defaults.
    float dwa_level{45};
  };
  // One extra header entry. type is string, int, float, double, rational (two
  // integers), int2 or float2; numbers hold the numeric value(s).
  struct OutputAttribute {
    std::string name, type, text;
    std::vector<double> numbers;
  };
  struct OutputOptions {
    bool half_color{}, crop_to_region{}, multipart{};
    bool fail_on_missing_inputs{};           // Final output only; IPR remains repairable.
    std::string existing_files{"overwrite"}; // overwrite, skip or error; enforced at publication.
    std::string compression{"zip"}; // Explicit codecs and per-pass channel precision are honored for all passes.
    float dwa_level{45};
    float pixel_aspect{};                         // Zero inherits the rendered frame.
    std::string color_space, color_configuration; // Empty keeps scene-linear RGB.
    std::vector<OutputPass> passes;               // Empty writes every explicitly enabled pass.
    bool alpha{true};                             // PNG/TIFF only: write the film's alpha channel.
    std::vector<OutputAttribute> metadata;        // Extra header entries for every part and file.
  };
  // The filename extension selects the format: .png, .jpg/.jpeg and .tif/.tiff
  // write the beauty image only; every other name writes EXR as before.
  void write_exr(const std::string &filename) const;
  void write_exr(const std::string &filename, const OutputOptions &options) const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

SceneUpdate calibration_fixture();
const char *state_name(SessionState state);
} // namespace cycles_houdini
