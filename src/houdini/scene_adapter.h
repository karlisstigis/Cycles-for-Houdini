// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "material_assignment.h"
#include "material_snapshot.h"
#include "material_stylesheets.h"
#include "geometry_selection.h"
#include "size_attribute.h"
#include "point_instancer.h"
#include <map>
#include <memory>
#include <set>
#include <vector>

class GU_Detail;
namespace cycles_houdini {
// ROP / object "Deformation": Off (TransformOnly), Velocity, Acceleration,
// Geometry Samples (Deformation) and Velocity if Available. The last one never
// reaches geometry conversion: NativeSceneAdapter::prepare resolves it per SOP
// output to Velocity or Deformation (docs/dev/notes/native-geometry-ownership.md).
enum class NativeGeometryMotion { TransformOnly, Velocity, Deformation, Acceleration, VelocityIfAvailable };
// Velocity and Acceleration read only current-frame attributes (v, w, accel):
// no SOP is cooked at other shutter times. Acceleration also bends the path.
constexpr bool attribute_motion(NativeGeometryMotion motion) {
  return motion == NativeGeometryMotion::Velocity || motion == NativeGeometryMotion::Acceleration;
}
enum class NativePointRenderMode { Auto, Spheres, GaussianSplats };
struct NativeMotionSettings {
  bool enabled{};
  bool vectors{}; // Explicit frame-1/frame/frame+1 samples, independently of shutter blur.
  double frames_per_second{24}, shutter_frames{0.5}, shutter_offset{};
  int samples{3};
  bool operator==(const NativeMotionSettings &) const = default;
};
struct NativeMotionWindow {
  bool enabled{};
  double nominal{}, centre{}, shutter_frames{};
  std::vector<double> times;
  std::vector<double> deltas; // Relative seconds, computed without subtracting large absolute times.
  bool vectors{};
};
NativeMotionWindow native_motion_window(double time, const NativeMotionSettings &settings);
// Owned transform provenance permits navigation against accepted geometry while
// another adapter transaction is running. Locals are captured directly, never
// recovered by inverting a possibly singular object matrix.
// Copy-on-write ordered object table. Reads never copy. Every write goes
// through edit(), which first takes a private copy when the table is shared,
// so a table handed out by share() (to an immutable scene snapshot) never
// changes afterwards.
class NativeObjectTable {
public:
  using Table = std::map<std::string, ObjectData>;
  using const_iterator = Table::const_iterator;
  const Table &view() const { return table_ ? *table_ : empty_table(); }
  Table &edit() {
    ++revision_;
    if (!table_)
      table_ = std::make_shared<Table>();
    else if (table_.use_count() != 1)
      table_ = std::make_shared<Table>(*table_);
    return *table_;
  }
  std::shared_ptr<const Table> share() const {
    if (!table_)
      table_ = std::make_shared<Table>();
    return table_;
  }
  bool owns(const std::shared_ptr<const Table> &table) const { return table_ && table_ == table; }
  // Every write access (edit() and the writers built on it) bumps the
  // revision. Identity plus revision prove that a table recorded earlier was
  // neither replaced nor written since (no insert, erase or value write).
  std::uint64_t revision() const { return revision_; }
  const Table *identity() const { return table_.get(); }
  // No snapshot and no other wrapper shares the table.
  bool exclusive() const { return table_ && table_.use_count() == 1; }
  const_iterator begin() const { return view().begin(); }
  const_iterator end() const { return view().end(); }
  const_iterator find(const std::string &key) const { return view().find(key); }
  const_iterator lower_bound(const std::string &key) const { return view().lower_bound(key); }
  const ObjectData &at(const std::string &key) const { return view().at(key); }
  bool contains(const std::string &key) const { return view().contains(key); }
  std::size_t size() const { return view().size(); }
  bool empty() const { return view().empty(); }
  template <class... Arguments> auto emplace(Arguments &&...arguments) {
    return edit().emplace(std::forward<Arguments>(arguments)...);
  }
  template <class... Arguments> auto try_emplace(Arguments &&...arguments) {
    return edit().try_emplace(std::forward<Arguments>(arguments)...);
  }
  // Erasing an absent key changes nothing: no write access, no copy.
  std::size_t erase(const std::string &key) { return contains(key) ? edit().erase(key) : 0; }

private:
  static const Table &empty_table() {
    static const Table empty;
    return empty;
  }
  mutable std::shared_ptr<Table> table_;
  std::uint64_t revision_{};
};
struct NativeNavigationState {
  struct Origin {
    std::string root;
    Matrix local{identity_matrix};
    std::vector<Matrix> motion;
  };
  using Origins = std::map<std::string, Origin>;
  struct Root {
    std::string path;
    NativeMotionWindow window;
    Matrix nominal{identity_matrix}, centre{identity_matrix};
    std::vector<Matrix> motion;
  };
  double time{};
  NativeMotionSettings motion;
  std::map<std::string, Root> roots;
  std::shared_ptr<const Origins> origins;
};
// Link mode: Blender's per-object values for one exported Blender object or instance, as
// Blender's Cycles syncs them (blender/object.cpp sync_object; docs/dev/blend-import.md
// "Per-object values"). Combined with the Houdini object's own values by
// apply_blender_object (scene_adapter.cpp).
struct BlenderObjectValues {
  std::uint32_t random_id{};
  std::uint32_t visibility{0xffu}; // Cycles ray visibility bits, as GeometryRequest::visibility.
  bool holdout{}, shadow_catcher{};
  int pass_index{};
  Float3 color{};
  float alpha{};
  std::vector<UniformAttribute> attributes; // Object/Instancer Attribute node fields.
  bool operator==(const BlenderObjectValues &) const = default;
};
struct GeometryRequest {
  struct MaterialSample {
    double time{};
    NativeMaterialAssignment assignment;
    std::string stylesheet;
    std::vector<std::string> errors, resources;
    bool operator==(const MaterialSample &) const = default;
  };
  struct MaterialSamples {
    using Values = std::vector<MaterialSample>;
    std::shared_ptr<const Values> values;
    const Values &data() const {
      static const Values empty;
      return values ? *values : empty;
    }
    auto begin() const { return data().begin(); }
    auto end() const { return data().end(); }
    bool empty() const { return !values || values->empty(); }
    void clear() { values.reset(); }
    bool operator==(const MaterialSamples &other) const { return values == other.values || data() == other.data(); }
  };
  std::string sop_path;
  std::string geometry_key;
  std::string object_key;
  std::string material_key;
  NativeMaterialMode material_mode{NativeMaterialMode::Reject};
  std::string object_material_path;
  std::vector<std::string> object_material_overrides; // Inherited packed binding, owned.
  std::vector<std::string> object_surface_overrides, object_displacement_overrides;
  NativeMaterialAssignment material_assignment() const {
    return {object_material_path, object_material_overrides, object_surface_overrides, object_displacement_overrides};
  }
  void set_material_assignment(const NativeMaterialAssignment &value) {
    object_material_path = value.path;
    object_material_overrides = value.overrides;
    object_surface_overrides = value.surface_overrides;
    object_displacement_overrides = value.displacement_overrides;
  }
  std::string stylesheet; // Owned object-filtered or packed-filtered native JSON.
  std::vector<std::string> stylesheet_errors;
  std::vector<std::string> stylesheet_resources;
  MaterialSamples object_material_samples;
  std::string object_material_revision;
  std::shared_ptr<const NativeMaterialStyles> native_styles;
  std::optional<NativeMaterialReference> native_fallback;
  std::shared_ptr<const NativeMaterialLookup> native_materials;
  std::shared_ptr<const NativeMaterialPolicy> native_material_policy;
  Matrix transform{identity_matrix};
  std::uint32_t random_id{};
  std::uint32_t visibility{0xffffffffu};
  bool smooth{};
  SubdivisionSettings subdivision;
  float point_radius{0.05f};
  NativePointRenderMode point_render_mode{NativePointRenderMode::Auto};
  bool point_splat_default_material{}; // ROP fallback is not an authored splat shader.
  float curve_width{0.001f};
  CurveShape curve_shape{CurveShape::Linear};
  int curve_segments{16};                                         // Deterministic per nonzero Bezier/NURBS knot span.
  std::array<bool, 5> render_types{true, true, true, true, true}; // Mesh, points, curves, volume, packed.
  ObjectProperties properties;
  bool volume_object_space{};
  std::string object_path; // Public OBJ evaluation at shutter times; empty means an explicit static matrix.
  NativeGeometryMotion motion{NativeGeometryMotion::TransformOnly};
  bool motion_blur{true}; // Object override can disable all shutter sampling.
  bool motion_explicit{}; // The object itself chose `motion` (not From ROP): report missing v.
  bool pointiness{};      // Intrinsic demand; never an attribute-name convention.
  std::set<std::string> shader_attributes;
  AttributeLayout instance_attributes;
  // Immutable external geometry bypasses Houdini SOP/GU mesh conversion.
  std::shared_ptr<const MeshData> external_mesh;
  std::vector<std::string> external_material_paths;
  std::optional<BlenderObjectValues> blender_object; // Link members only.
  NativePointInstancingRef point_instancing;         // Instance OBJ / s@instance: the SOP holds instancing points.
  // Blender references serialized in native packed primitives. The same immutable
  // external member table is used by the point shortcut and packed traversal.
  NativePointInstancingRef packed_blender;
  // Several requests may read one SOP (geometry plus its s@instance points); roots are keyed per role.
  std::string source_key() const { return point_instancing ? sop_path + "#point-instances" : sop_path; }
  float volume_step_size{}; // Object-space ray-march step; zero derives it from grid resolution.
  bool operator==(const GeometryRequest &) const = default;
};
std::string subdivision_signature(const SubdivisionSettings &settings);
void subdivision_identifiers(std::vector<std::int64_t> &identifiers, const SubdivisionSettings &settings);
struct NativeMeshCaptureCache;
struct PreparedVolume;
struct VolumeConversionCache;
class PreservedDetail;
class NativeMaterialMotionCache;
class NativePackedPreparationCache;
struct NativePackedGroupingCache;
struct NativeInstanceFieldShutter;
struct NativeSceneRoots {
  struct Input {
    std::shared_ptr<const PreservedDetail> detail;
    std::optional<SourceDiagnostic> diagnostic;
    std::array<bool, 5> native_types{};
    std::vector<SourceDiagnostic> warnings; // Point-instance synthesis diagnostics.
    std::shared_ptr<const NativePointInstanceSample> point_sample;
    bool packed{}, scanned{}, expanded{};
    unsigned depth{};
    std::size_t next_packed{};
  };
  std::map<std::string, Input> inputs;
  std::map<std::int64_t, Input> prototypes;
  std::map<std::pair<std::int64_t, GA_Offset>, std::shared_ptr<const PreservedDetail>> packed_details;
  struct ShutterInput {
    bool acquired{};
    std::shared_ptr<const PreservedDetail> detail;
    std::string warning;
  };
  mutable std::map<std::pair<std::string, double>, ShutterInput> shutter_inputs;
  double cooking_seconds{};
};
std::shared_ptr<NativeSceneRoots> acquire_scene_roots(const std::vector<GeometryRequest> &requests, double time,
                                                      NativePointInstanceCache *instances = nullptr);
struct NativeSceneLifecycleStats {
  double prepare_seconds{}, publication_seconds{}, retirement_seconds{};
  std::uint64_t publications{}, retirements{}, retired_scenes{}, retired_objects{}, retired_geometry{},
      last_retired_objects{}, retired_snapshots{}, retired_snapshot_objects{};
  bool retirement_on_host{};
};
struct ExtractionStats {
  std::vector<std::string> motion_warnings;
  double cooking_seconds{}, conversion_seconds{}, shutter_open_seconds{}, shutter_close_seconds{},
      component_check_seconds{};
  std::uint64_t extractions{}, cache_hits{}, packed_instances{}, index_instance_ids{}, points{}, curves{},
      index_primitive_ids{}, motion_samples{}, motion_cooks{}, material_binding_scans{}, material_binding_cache_hits{},
      partition_scans{}, partition_cache_hits{}, partition_cache_bytes{}, component_checks{}, component_reuses{},
      mesh_topology_builds{}, mesh_topology_reuses{}, mesh_topology_scans{}, mesh_triangulated_faces{},
      mesh_retained_faces{}, mesh_capture_cache_bytes{};
  std::uint64_t direct_instances_expanded{}, direct_population_reuses{}; // Per capture; owned direct population only.
  std::uint64_t packed_population_reuses{}; // Current accepted flat external population only.
  std::uint64_t adapter_full_object_updates{}, adapter_pose_updates{}, adapter_attribute_updates{},
      adapter_pose_attribute_updates{};
  // Per capture: objects written into a recycled generation (scene_adapter.cpp
  // convert, PackedLayout), or why the full path was used.
  std::uint64_t packed_recycled_objects{};
  std::string packed_recycle_fallback;
  std::uint64_t material_motion_cooks{}, material_motion_samples{}, material_motion_cache_hits{};
  std::uint64_t dense_field_conversions{}, retained_dense_voxel_bytes{};
  std::uint64_t mesh_named_fields_captured{}, mesh_named_fields_reused{}, mesh_named_values_read{},
      mesh_named_values_reused{};
  std::map<std::string, double> packed_assembly_seconds; // Opt-in conversion phases; excludes cooking/device/paint.
  std::size_t packed_template_components{}, packed_expanded_components{}, packed_final_components{},
      packed_deformation_tasks{}, packed_profile_groups{};
  NativeSceneLifecycleStats lifecycle;
};
class NativeSceneAdapter {
public:
  NativeSceneAdapter() = default;
  NativeSceneAdapter(const NativeSceneAdapter &) = delete;
  NativeSceneAdapter &operator=(const NativeSceneAdapter &) = delete;
  struct PreparedScene;
  // Host-only acquisition. Owns retained geometry, packed resolutions, VDB
  // headers and shutter samples. No host callback survives preparation.
  std::shared_ptr<PreparedScene> prepare(const std::vector<GeometryRequest> &requests, double time,
                                         bool authoritative = true, const NativeMotionSettings &motion = {},
                                         const NativeMaterialPreparation &prepare_materials = {},
                                         const std::shared_ptr<const NativeSceneRoots> &roots = {},
                                         NativePreparationBudget *budget = nullptr);
  // Pure scans on retained immutable inputs, under exclusive adapter ownership.
  void prewarm_roots(NativeSceneRoots &roots);
  // Resolve one scanned packing frontier on the host. True schedules another
  // background scan; no uncached prototype classification happens here.
  bool acquire_packed_roots(NativeSceneRoots &roots);
  // Exclusive adapter ownership must pass to this call until it returns.
  // Conversion may run off-thread; no other adapter method may run in parallel.
  // Each prepared transaction is consumed once, by its originating adapter.
  SceneUpdate convert(const std::shared_ptr<PreparedScene> &prepared);
  std::shared_ptr<const NativeNavigationState> navigation() const {
    return pending_ ? pending_->navigation : active_.navigation;
  }
  // Invoke only in Houdini's supported cook context on the host thread.
  SceneUpdate capture(const std::vector<GeometryRequest> &requests, double time, bool authoritative = true,
                      const NativeMotionSettings &motion = {}, const NativeMaterialPreparation &prepare_materials = {});
  void invalidate() {
    ++preparation_serial_;
    point_instance_cache_->clear();
    cache_.clear();
    prototype_aliases_.clear();
    prototype_contents_.clear();
    active_.instance_fields.clear();
    active_.direct_population.reset();
    active_.packed_population.reset();
    spare_.reset();
    binding_cache_.clear();
    style_cache_.clear();
    style_binding_cache_.clear();
    partition_cache_.clear();
    size_cache_.clear();
    material_motion_cache_.reset();
    packed_preparation_cache_.reset();
    packed_grouping_cache_.reset();
    if (pending_)
      retired_.swap(pending_);
    if (retired_)
      retired_->packed_layout.reset();
    pending_.reset();
    pending_ready_ = false;
    pending_population_reuse_ = false;
  }
  void reset() {
    invalidate();
    binding_cache_.reset();
    partition_cache_.reset();
    active_ = {};
    release_retired();
    material_paths_.clear();
    stylesheet_resources_.clear();
    stats_.motion_warnings.clear();
    stats_.packed_instances = stats_.index_instance_ids = stats_.points = stats_.curves = stats_.index_primitive_ids =
        stats_.motion_samples = stats_.motion_cooks = 0;
    stats_.shutter_open_seconds = stats_.shutter_close_seconds = 0;
    stats_.direct_instances_expanded = stats_.direct_population_reuses = 0;
    stats_.packed_population_reuses = 0;
    stats_.adapter_full_object_updates = stats_.adapter_pose_updates = stats_.adapter_attribute_updates =
        stats_.adapter_pose_attribute_updates = 0;
    stats_.packed_recycled_objects = 0;
    stats_.packed_recycle_fallback.clear();
    stats_.lifecycle = {};
  }
  // Exclusive converter ownership: reconcile physical deletions and prune
  // memoization before publication. Failure leaves the active scene intact.
  void prepare_commit(const SceneUpdate &update);
  // Accept only reconciled state. The prior generation occupies one bounded
  // retirement slot, drained by the next exclusive scan/conversion or close.
  void commit();
  // Caller keys identify a logical SOP source. One source may own several
  // physical engine resources; explicit deletions preserve that contract.
  void expand_deletions(SceneUpdate &update) const;
  ExtractionStats stats() const;
  // The object table of the prepared (or, without one, accepted) scene, when
  // it provably equals snapshot_state(previous, update).objects: the previous
  // snapshot holds the accepted table itself (or both are empty) and no
  // deleted key is also upserted, posed or attributed in this update. Null
  // otherwise; the caller then builds the snapshot table from the update.
  std::shared_ptr<const NativeObjectTable::Table>
  snapshot_objects(const std::shared_ptr<const NativeObjectTable::Table> &previous, const SceneUpdate &update) const;
  void record_profile_phase(const char *name, double seconds) { stats_.packed_assembly_seconds[name] += seconds; }
  NativePointInstanceCache &point_instances() { return *point_instance_cache_; }
  std::vector<std::string> object_keys() const {
    std::vector<std::string> keys;
    for (const auto &[key, value] : active_.objects)
      keys.push_back(key);
    return keys;
  }
  std::vector<NativeMaterialReference> material_references() const;
  std::vector<std::string> material_paths() const { return {material_paths_.begin(), material_paths_.end()}; }
  std::vector<std::string> stylesheet_resources() const {
    return {stylesheet_resources_.begin(), stylesheet_resources_.end()};
  }

private:
  // Pure assembly of acquired metadata; exclusive adapter ownership, no OP evaluation.
  void assemble_packed(const std::shared_ptr<PreparedScene> &prepared);
  void assemble_instance_attributes(const std::shared_ptr<PreparedScene> &prepared);
  void release_retired();
  struct PackedLayout;
  struct PackedSpare;
  std::shared_ptr<PackedSpare> spare_;
  struct Record {
    bool volume_available{true};
    std::string motion_warning;
    std::vector<SourceDiagnostic> source_diagnostics;
    std::vector<std::int64_t> identifiers;
    std::optional<std::array<std::uint64_t, 2>> component_signature;
    std::uint64_t revision{}, primitive_count{}, index_ids{};
    std::string material_key;
    std::vector<NativeMaterialReference> material_references;
    std::set<std::string> shader_attributes;
    std::shared_ptr<NativeMeshCaptureCache> mesh_capture;
    std::shared_ptr<const MeshData> external_mesh;
    std::shared_ptr<const MeshData> external_render_mesh;
    std::shared_ptr<const PreparedVolume> volume_capture;
    AttributeLayout instance_attributes;
    std::shared_ptr<VolumeConversionCache> volume_conversion;
    std::vector<std::pair<std::uintptr_t, std::uint64_t>> volume_source_allocations;
  };
  std::map<std::string, Record> cache_;
  NativeMaterialBindingCache binding_cache_;
  NativeStyleCache style_cache_;
  NativeStyleBindingCache style_binding_cache_;
  NativePartitionCache partition_cache_;
  NativeSizeCache size_cache_;
  std::shared_ptr<NativeMaterialMotionCache> material_motion_cache_;
  std::shared_ptr<NativePackedPreparationCache> packed_preparation_cache_;
  std::shared_ptr<NativePackedGroupingCache> packed_grouping_cache_;
  std::shared_ptr<NativePointInstanceCache> point_instance_cache_ = std::make_shared<NativePointInstanceCache>();
  std::set<std::string> material_paths_;
  std::set<std::string> stylesheet_resources_;
  ExtractionStats stats_;
  std::uint64_t next_revision_{};
  std::uint64_t preparation_serial_{};
  // Content-addressed leaf packed prototypes (scene_adapter.cpp
  // canonical_prototype): a SOP recook gives identical embedded prototypes new
  // GU detail ids; their geometry keys follow the content instead.
  struct PrototypeAlias {
    std::vector<std::int64_t> stamp;
    std::int64_t canonical{};
    std::array<std::uint64_t, 2> content{};
    bool hashed{};
    std::uint64_t epoch{};
  };
  std::map<std::int64_t, PrototypeAlias> prototype_aliases_;
  std::map<std::array<std::uint64_t, 2>, std::pair<std::int64_t, std::uint64_t>> prototype_contents_;
  std::uint64_t prototype_epoch_{};
  std::int64_t canonical_prototype(const GU_Detail &detail);
  struct RetainedComponent {
    NativeGeometryKind kind;
    std::string key, base_key, source;
    std::set<std::string> shader_attributes;
    std::uint64_t multiplicity{1};
    bool pointiness{};
  };
  struct PackedDeformationState {
    std::vector<std::int64_t> identifiers;
    std::array<std::uint64_t, 2> layout{};
    std::set<std::string> recovered;
    std::string cook_warning;
    GeometryRequest request; // Centre OBJ transform is excluded from this cache key.
    std::vector<RetainedComponent> components;
    std::vector<SourceDiagnostic> diagnostics;
    std::uint64_t packed_instances{}, index_ids{};
    bool stable_packed_sources{};
  };
  struct DirectPopulationState {
    std::shared_ptr<const NativePointInstanceSample> sample;
    std::vector<std::string> bindings;
    std::string root, geometry_alias;
    Matrix transform;
    ObjectData model;
    std::map<std::string, std::pair<std::shared_ptr<const MeshData>, std::shared_ptr<const MeshData>>> meshes;
    bool operator==(const DirectPopulationState &) const = default;
  };
  struct PackedPopulationState {
    struct Binding {
      GeometryRequest member;
      NativeMaterialAssignment assignment;
      std::vector<UniformAttribute> fields; // Only layout is consumed by external binding resolution.
      std::string key;
    };
    // Keeping the preserve lease makes pointer identity an immutable-content
    // proof, including all packed transforms and numeric shader attributes.
    std::shared_ptr<const PreservedDetail> detail;
    GeometryRequest request;
    std::vector<Binding> bindings;
    std::map<std::string, std::pair<std::shared_ptr<const MeshData>, std::shared_ptr<const MeshData>>> meshes;
    std::uint64_t instances{}, index_ids{}, objects{};
  };
  struct Resources {
    struct InstanceFields {
      std::vector<std::int64_t> identifiers;
      std::set<std::string> names;
      std::string source;
      unsigned depth{};
      std::shared_ptr<const NativeInstanceFieldShutter> shutter;
    };
    std::map<std::string, InstanceFields> instance_fields;
    std::shared_ptr<NativeNavigationState> navigation;
    std::set<std::string> geometry;
    // Requested components omitted by source recovery still own bounded
    // conversion metadata. Engine retirement must not invalidate that cache.
    std::set<std::string> unavailable_geometry;
    NativeObjectTable objects;
    std::set<std::int64_t> binding_details;
    std::set<std::int64_t> partition_details;
    std::map<std::string, std::set<std::string>> geometry_aliases, object_aliases;
    std::map<std::string, std::set<std::int64_t>> geometry_details;
    // These identities belong to the committed scene, independently of
    // disposable extraction metadata. Failed capture must not retire them.
    std::map<std::string, std::vector<NativeMaterialReference>> native_materials;
    std::map<std::string, PackedDeformationState> packed_deformations;
    std::optional<DirectPopulationState> direct_population;
    std::optional<PackedPopulationState> packed_population;
    // Node addresses of this generation's object/origin tables by packed
    // slot, for recycling (scene_adapter.cpp PackedLayout). Reset whenever
    // these containers are copied or changed outside convert().
    std::shared_ptr<PackedLayout> packed_layout;
  };
  Resources active_;
  std::optional<Resources> pending_;
  std::optional<Resources> retired_;
  bool pending_ready_{};
  bool pending_population_reuse_{};
};
CameraData capture_camera(const std::string &path, double time, int width, int height, float pixel_aspect = 1);
CameraData capture_camera(const std::string &path, const NativeMotionWindow &window, int width, int height,
                          float pixel_aspect = 1);
Matrix capture_object_transform(const std::string &path, double time);
Matrix capture_sop_object_transform(const std::string &path, double time);
} // namespace cycles_houdini
