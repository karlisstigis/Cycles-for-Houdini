// SPDX-License-Identifier: Apache-2.0
#pragma once
// Private engine state and phase contracts. Callers hold session control.
// Engine mutation phases additionally hold scene->mutex; no
// engine callback may be live while the transaction mutates accepted resources.
#include "session_utils.h"
#include "util/tbb.h"
#include <cstdlib>
#include <cstring>
#include <unordered_map>
namespace cycles_houdini {
using namespace session_detail;
struct RenderSession::Impl {
  // Caller holds this session's control lock; never visits another owner.
  void invalidate_engine_color(std::uint64_t next_epoch);
  enum class GeometryKind { Mesh, Volume, Points, Curves };
  struct VolumeVectorRecord {
    std::array<openvdb::FloatGrid::ConstPtr, 3> sources;
    openvdb::Vec3SGrid::ConstPtr grid;
    std::size_t bytes{};
    std::array<std::uint64_t, 3> revisions{};
  };
  using VolumeVectors = std::map<std::string, VolumeVectorRecord>;
  struct MeshRenderState {
    std::string primary_variant;
    std::map<std::string, ccl::Mesh *> variants;
    std::map<ccl::Mesh *, std::shared_ptr<SubdivisionMetrics>> subdivision_metrics;
    std::shared_ptr<const MeshData> source;
  };
  struct MeshFamilyMembers {
    std::string leader;
    std::vector<std::string> members;
  };
  struct MeshRecord {
    ccl::Geometry *node{};
    GeometryKind kind{GeometryKind::Mesh};
    std::uint64_t revision{};
    std::size_t bytes{}, source_bytes{}, seam_source_bytes{}, nominal_source_bytes{};
    std::vector<std::string> materials;
    std::shared_ptr<const VolumeData> volume;
    VolumeVectors volume_vectors;
    Float3 volume_motion_bounds{};
    // Cycles displaces positions in place. Keep an independent, immutable
    // source, including motion and corner attributes, for subsequent edits.
    std::shared_ptr<const MeshData> source;
    std::shared_ptr<const MeshData> seam_source;
    std::vector<int> seam_vertices; // Render vertex -> canonical vertex; Generated keeps nominal coordinates.
    std::shared_ptr<const MeshData> nominal_source;
    // One mutable physical state and one immutable member list per family.
    // Logical aliases must never copy the physical variant/metric maps.
    std::shared_ptr<MeshRenderState> render;
    bool dirty{}, displaced{}, has_uv{}, has_color{}, generated{}, deforming{}, omit_deformation{},
        sampled_displacement{};
    bool shading_dirty{}, force_source_update{}, gaussian_splats{};
    std::vector<std::string> displacement_slots; // Value stamps survive shader mutation/recovery.
    std::set<std::string> frozen_temporal_materials;
    AttributeLayout attributes, instance_attributes;
    // Normalized local coordinates survive Cycles' in-place transform of
    // point/hair positions, so a material edit never recooks those SOPs.
    std::vector<ccl::packed_float3> strand_generated;
    bool strand_generated_fallback{}; // Link Curves: no Generated attribute (object-space position).
    std::vector<float> island_random; // Canonical face values, shared by render variants.
    std::vector<float> pointiness;    // Canonical vertex values before render seams.
    bool islands{}, has_pointiness{};
    bool pixel_adaptive_subdivision() const {
      return source && source->subdivision && source->subdivision->settings.adaptive &&
             source->subdivision->settings.space == SubdivisionSpace::Pixel;
    }
    // Logical binding contexts stay independent. Only the engine geometry
    // and its complete shader demand are shared by a compatible family.
    std::shared_ptr<const MeshFamilyMembers> sharing_family;
    std::shared_ptr<const MeshData> sharing_source, hashed_source;
    std::uint64_t shape_hash{};
    std::vector<ccl::Shader *> surface_palette, sharing_palette;
    bool sharing_generated{}, sharing_islands{}, sharing_pointiness{};
  };
  struct LightRecord {
    ccl::Light *node{};
    ccl::Object *object{};
    ccl::Shader *cone_shader{};
    LightKind kind{};
    std::optional<LightData> definition;
  };
  struct ObjectRecord {
    ccl::Object *node{};
    std::string geometry_key;
    std::vector<Matrix> motion;
    Matrix matrix{identity_matrix}, render_matrix{identity_matrix};
    std::optional<Matrix> nominal_transform;
    bool omit_transform{};
    std::uint32_t random_id{}, visibility{0xffffffffu};
    ObjectProperties properties;
    std::vector<UniformAttribute> attributes;
    std::uint64_t attribute_bytes{};
    std::size_t uniform_displacement_steps{};
    std::set<std::string> displacement_attribute_holds;
    std::optional<Matrix> dicing_matrix;
  };
  SessionConfig config;
  // Refit Meshes While Interacting: refits in a row per mesh since its last build; a mesh is
  // rebuilt after mesh_refit_limit refits so a long drag cannot keep a degraded structure.
  static constexpr int mesh_refit_limit = 16;
  std::unordered_map<const ccl::Geometry *, int> mesh_refit_streak;
  bool mesh_refit_enabled() const {
    // CYCLES_HOUDINI_BVH_REFIT=1 turns refit on for tests regardless of the ROP; live renders only.
    static const bool forced = [] {
      const char *value = std::getenv("CYCLES_HOUDINI_BVH_REFIT");
      return value && std::strcmp(value, "1") == 0;
    }();
    return config.interactive && (config.refit_meshes || forced);
  }
  DenoisePolicy denoise_policy;
  RenderSettings rendering;
  ccl::SessionParams params;
  // Declared before Session so its files outlive engine cancellation/destruction.
  std::shared_ptr<std::filesystem::path> tile_storage;
  std::unique_ptr<ccl::Session> session;
  std::shared_ptr<FrameStore> store{std::make_shared<FrameStore>()};
  mutable std::mutex control;
  std::map<std::string, ccl::Shader *> materials;
  // The pinned Scene::delete_nodes<Shader> deliberately retains shader
  // nodes. Reuse unbound slots instead of appending one per assignment edit.
  // This retains a documented high-water mark until session shutdown.
  std::vector<ccl::Shader *> retired_shaders;
  std::uint64_t shader_reuses{};
  std::set<ccl::Shader *> deferred_retired_shaders;
  std::map<std::string, MaterialData> material_definitions;
  bool has_temporal_materials{};
  std::vector<SourceDiagnostic> source_diagnostics;
  std::vector<SourceDiagnostic> displacement_motion_diagnostics;
  std::set<std::string> temporal_diagnostic_materials;
  std::map<std::string, ShaderGraphRequirements> material_requirements;
  std::map<std::string, ShaderGraphRequirements> light_requirements;
  // Shader-only variants preserve source identities and packed prototypes.
  // They exist only while a binding lacks UV/color data or required volume fields.
  std::map<BindingKey, ccl::Shader *> binding_materials;
  std::map<BindingKey, ShaderGraphRequirements> recovered_requirements;
  // Set by sync_material_bindings for the current transaction: every material edit only changed shading values
  // (same geometry requirements, no true displacement, binding shaders kept). finish_geometry_sync then skips
  // mesh regrouping, variant sync and displacement-motion recovery for it.
  bool material_values_only{};
  // The last displacement-motion recovery found nothing to recover (no true displacement, no recovery state on
  // any mesh or object). Only recovery itself creates such state, so a pose-only update keeps it.
  bool displacement_recovery_idle{};
  using VolumeRecovery = std::map<std::string, std::vector<SourceDiagnostic>>;
  VolumeRecovery volume_recovery;
  std::map<std::string, std::vector<std::string>> texture_watch_paths;
  std::map<std::string, bool> texture_watch_auto;
  std::map<std::string, std::string> texture_watch_revisions;
  std::map<std::string, std::vector<std::string>> light_watch_paths;
  std::map<std::string, bool> light_watch_auto;
  std::map<std::string, std::string> light_watch_revisions;
  std::map<std::string, MeshRecord> meshes;
  ccl::set<ccl::Geometry *> pending_geometry_retirement;
  std::shared_ptr<ccl::SubdivisionShadingBudget> subdivision_shading_budget{
      std::make_shared<ccl::SubdivisionShadingBudget>(256ull * 1024 * 1024)};
  std::map<std::string, ObjectRecord> objects;
  // Exact retained UniformAttribute storage, updated with object ownership.
  std::uint64_t object_attribute_bytes{};
  std::map<std::string, LightRecord> lights;
  Crop crop;
  SessionState state{SessionState::Idle};
  std::uint64_t mesh_refits{};
  std::uint64_t generation{}, geometry_conversions{}, object_updates{}, displacement_rebuilds{}, unchanged_reuses{},
      volume_vector_merges{}, direct_attribute_updates{}, expanded_attribute_updates{}, attribute_fallback_count{};
  std::string attribute_fallback_reason;
  double volume_vector_assembly_seconds{};
  std::atomic<std::uint64_t> subdivision_tessellations{};
  double last_update_seconds{};
  double cancel_join_seconds{}, shader_graph_preparation_seconds{}, scene_boundary_wait_seconds{};
  std::uint64_t session_cancel_calls{};
  std::uint64_t cancel_output_deliveries{}, cancel_display_updates{};
  std::uint64_t cancel_output_reads_skipped{}, cancel_display_updates_skipped{};
  double cancel_output_delivery_seconds{}, cancel_display_conversion_seconds{};
  std::uint64_t material_builds{}, light_updates{}, background_builds{};
  std::optional<Float3> background_definition;
  std::optional<WorldData> world_definition;
  LinkingSettings linking_definition;
  LinkingPlan linking_plan;
  bool world_surface_emission{};
  ShaderGraphRequirements world_requirements;
  std::vector<std::string> world_watch_paths;
  bool world_watch_auto{};
  std::string world_watch_revision;
  std::optional<std::string> background_lightgroup_definition;
  std::optional<bool> background_shadow_catcher_definition;
  ccl::Object *background_object{}; // Owned by the Cycles scene, like other engine nodes.
  std::optional<CameraData> camera_definition;
  ImageIrisPtr image_iris;
  std::string image_iris_watch_path, image_iris_watch_revision;
  std::uint64_t image_iris_preparations{};
  std::optional<CameraData> dicing_camera_definition;
  DicingSettings dicing_settings;
  void set_topology_cache_limit(std::uint32_t mib);
  float applied_offscreen_scale{1};
  bool automatic_camera_refinement{};
  // A coarse navigation preview must be refined even if the user returns
  // to the projection the cancelled tessellation had already accepted.
  bool suspended_camera_refinement{};
  std::optional<bool> transparent_background_definition, transparent_glass_definition, motion_blur_definition;
  std::vector<std::string> render_device_ids;
  std::optional<float> transparent_roughness_definition;
  bool paused{};
  bool color_update_pending{};
  std::shared_ptr<const ColorConfiguration> source_color;
  std::shared_ptr<const ColorConfiguration> target_color;
  std::uint64_t source_color_epoch{}, target_color_epoch{};
  std::vector<Matrix> camera_motion;
  bool motion_vectors() const;
  bool sampled_transforms() const;
  bool interactive_denoising_motion() const;
  ccl::array<ccl::Transform> denoising_motion(const ccl::array<ccl::Transform> &previous,
                                              const ccl::Transform &current) const;

  explicit Impl(const SessionConfig &c, std::shared_ptr<const ColorConfiguration> color);
  void ensure_open() const;
  DenoisePolicy resolve_session_denoising(const DenoiseSettings &settings,
                                          const std::vector<std::string> *passes = nullptr,
                                          std::optional<bool> motion_blur = std::nullopt) const;
  void configure_denoising(const DenoiseSettings &settings, const DenoisePolicy &policy, bool force_passes = false);
  void cancel_engine();
  bool pending_camera_tessellation() const;
  void validate(const SceneUpdate &u, std::map<std::string, Float3> &volume_bounds,
                const std::map<std::string, ShaderGraphRequirements> &graph_requirements,
                const std::map<BindingKey, ShaderGraphRequirements> &recovered, const VolumeRecovery &volume_fallbacks,
                std::vector<ObjectRecord *> &pose_records);
  void update_material(const MaterialData &m, ccl::unique_ptr<ccl::ShaderGraph> prepared = {},
                       bool camera_only_emission = false);
  void update_mesh(const MeshData &m);
  void update_nominal_source(MeshRecord &record);
  struct MeshHandoff {
    std::string previous;
    std::shared_ptr<const MeshData> source;
    std::vector<std::pair<std::string, std::string>> shaders; // Retired key -> incoming key, including shutter shaders.
  };
  struct BindingHandoffs {
    std::vector<MeshHandoff> meshes;
    std::map<std::string, std::string> shaders; // Incoming key -> retired key.
  };
  BindingHandoffs prepare_binding_handoffs(const SceneUpdate &update, const std::set<BindingKey> &recovered,
                                           const std::map<std::string, ShaderGraphRequirements> &requirements) const;
  std::set<ccl::Shader *> apply_binding_handoffs(const BindingHandoffs &handoffs);
  bool palette_generated(const std::vector<std::string> &palette) const;
  bool palette_islands(const std::vector<std::string> &palette) const;
  bool camera_displacement(const MeshRecord &record, bool matrix_changed, bool projection_changed) const;
  bool palette_pointiness(const std::vector<std::string> &palette) const;
  ccl::Shader *binding_shader(const std::string &key, bool has_uv, bool has_color, bool strands = false,
                              const AttributeLayout &attributes = {}, const AttributeLayout &instances = {}) const;
  struct MeshDisplacementState {
    std::vector<std::string> slots;
    DisplacementFields fields;
    bool active{}, transform{}, identity{}, object_index{};
  };
  MeshDisplacementState mesh_displacement_state(const MeshData &source, bool sampled,
                                                const std::set<std::string> &frozen) const;
  std::vector<ccl::Shader *> resolved_mesh_palette(const MeshRecord &record) const;
  bool surface_mesh_sharing_allowed(const MeshRecord &record) const;
  static MeshSharingFieldOwners field_owners(const MeshRecord &record);
  // Reconcile only after source/binding changes. Pose-only transactions reuse
  // the retained family without hashing or rescanning its geometry.
  void reconcile_mesh_sharing(std::set<std::string> &changed);
  void retire_unused_geometry();
  void mesh_palette(ccl::Mesh *mesh, const MeshData &source, bool bound, bool sampled, std::size_t steps,
                    const std::set<std::string> &frozen, const std::vector<ccl::Shader *> *shared = nullptr);
  void generated_attribute(ccl::Mesh &mesh, ccl::AttributeSet &attributes, const MeshData &source,
                           std::span<const int> vertices, std::size_t count);
  void pointiness_attribute(ccl::AttributeSet &attributes, std::span<const float> values, std::span<const int> vertices,
                            std::size_t count);
  void fill_mesh(ccl::Mesh *mesh, const MeshData &source, bool bound, const Matrix &matrix = identity_matrix,
                 const std::shared_ptr<SubdivisionMetrics> &metrics = {}, bool displaced = false,
                 bool sampled_displacement = false, std::size_t object_motion_steps = 0,
                 const std::set<std::string> &frozen_temporal = {}, const MeshData *generated_source = nullptr,
                 std::span<const int> generated_vertices = {}, std::span<const float> islands = {},
                 std::span<const float> pointiness = {}, const DisplacementFields &displacement_fields = {},
                 const std::vector<int> &displacement_classes = {}, const std::vector<ccl::Shader *> *shared = nullptr,
                 bool shared_generated = false);
  static std::shared_ptr<const MeshData> displacement_seams(const std::shared_ptr<const MeshData> &source,
                                                            std::vector<int> &generated_vertices,
                                                            const DisplacementFields &used,
                                                            const std::vector<int> &classes);
  void sync_mesh_variants(const std::set<std::string> &changed, bool refresh,
                          bool defer_interrupted_camera_subdivision);
  void recover_displacement_motion(std::set<std::string> &changed);
  void update_volume(std::shared_ptr<const VolumeData> data, const Float3 &motion_bounds, VolumeVectors vectors);
  template <typename Data>
  void update_point_attributes(ccl::Geometry *geometry, const Data &data, bool curves,
                               const std::vector<std::string> &palette) {
    // Pure copies and casts on fixed blocks of 16384 elements from index 0,
    // parallel over the block index on the engine's TBB pool;
    // CYCLES_HOUDINI_SERIAL_CONVERSION=1 runs the blocks on this thread.
    const auto blocks = [](std::size_t count, const auto &copy) {
      constexpr std::size_t block = 16384;
      const std::size_t total = (count + block - 1) / block;
      const auto run = [&](std::size_t b) { copy(b * block, std::min(count, b * block + block)); };
      const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
      if (total <= 1 || (serial && std::strcmp(serial, "1") == 0)) {
        for (std::size_t b = 0; b < total; ++b)
          run(b);
        return;
      }
      tbb::parallel_for(std::size_t(0), total, run);
    };
    {
      auto *p = geometry->get_position_for_write();
      const auto *positions = data.positions.data();
      blocks(data.positions.size(), [=](std::size_t begin, std::size_t end) {
        for (auto i = begin; i < end; ++i)
          p[i] = ccl::packed_float3(vec(positions[i]));
      });
      auto *r = geometry->get_radius_for_write();
      const auto *radii = data.radii.data();
      blocks(data.radii.size(),
             [=](std::size_t begin, std::size_t end) { std::copy(radii + begin, radii + end, r + begin); });
    }
    update_geometry_motion(geometry, data.motion, session->scene->integrator->get_motion_blur());
    fill_named_attributes(geometry->attributes, data.attributes,
                          curves ? ccl::ATTR_ELEMENT_CURVE_KEY : ccl::ATTR_ELEMENT_VERTEX, false, geometry);
    if (!data.colors.empty()) {
      auto *attr = geometry->attributes.add(ccl::ustring("Cd"), ccl::TypeColor,
                                            curves ? ccl::ATTR_ELEMENT_CURVE_KEY : ccl::ATTR_ELEMENT_VERTEX);
      auto *color = attr->template data_for_write<ccl::packed_float3>();
      const auto *colors = data.colors.data();
      blocks(data.colors.size(), [=](std::size_t begin, std::size_t end) {
        for (auto i = begin; i < end; ++i)
          color[i] = ccl::packed_float3(vec(colors[i]));
      });
      if (!data.motion_colors.empty()) {
        attr->add_motion(geometry);
        for (std::size_t step = 0; step < data.motion_colors.size(); ++step) {
          color = attr->template data_for_write<ccl::packed_float3>(int(step + 1));
          for (const auto &v : data.motion_colors[step])
            *color++ = ccl::packed_float3(vec(v));
        }
      }
    }
    if (!data.randoms.empty()) {
      auto *values = geometry->attributes.add(curves ? ccl::ATTR_STD_CURVE_RANDOM : ccl::ATTR_STD_POINT_RANDOM)
                         ->template data_for_write<float>();
      const auto *randoms = data.randoms.data();
      blocks(data.randoms.size(),
             [=](std::size_t begin, std::size_t end) { std::copy(randoms + begin, randoms + end, values + begin); });
    }
    if (!data.uv.empty()) {
      auto *attr = geometry->attributes.add(ccl::ATTR_STD_UV);
      auto *uv = attr->template data_for_write<ccl::float2>();
      const auto *uvs = data.uv.data();
      blocks(data.uv.size(), [=](std::size_t begin, std::size_t end) {
        for (auto i = begin; i < end; ++i)
          uv[i] = ccl::make_float2(uvs[i].x, uvs[i].y);
      });
      if (!data.motion_uv.empty()) {
        attr->add_motion(geometry);
        for (std::size_t step = 0; step < data.motion_uv.size(); ++step) {
          uv = attr->template data_for_write<ccl::float2>(int(step + 1));
          for (const auto &v : data.motion_uv[step])
            *uv++ = ccl::make_float2(v.x, v.y);
        }
      }
    }
    ccl::array<ccl::Node *> shaders;
    shaders.reserve(palette.size());
    for (const auto &key : palette) {
      auto *shader =
          binding_shader(key, !data.uv.empty(), !data.colors.empty(), true,
                         attribute_layout(data.attributes, data.instance_attributes), data.instance_attributes);
      shader->tag_used(session->scene.get());
      shaders.push_back_reserved(shader);
    }
    geometry->set_used_shaders(shaders);
    geometry->tag_position_modified();
    geometry->tag_radius_modified();
    geometry->tag_update(session->scene.get(), true);
  }
  void update_strand_generated(MeshRecord &record);
  void update_points(const PointData &data);
  void update_curves(const CurveData &data);
  void retire_shader(ccl::Shader *shader);
  void apply_linking(ccl::Object *object, const std::string &id);
  ccl::unique_ptr<ccl::ShaderGraph> light_cone_graph(const LightCone &cone,
                                                     ccl::unique_ptr<ccl::ShaderGraph> graph = {});
  void update_light(const LightData &l, ccl::unique_ptr<ccl::ShaderGraph> prepared = {});
  // Owns exclusively prepared resources until the synchronous engine boundary
  // consumes them. pose_records borrows accepted scene owners under control.
  struct PreparedUpdate {
    std::optional<CameraData> desired_camera{};
    DicingSettings desired_dicing{};
    bool force_dicing{};
    bool hold_dicing{};
    bool recalculate_dicing{};
    std::optional<CameraData> desired_dicing_camera{};
    bool dicing_projection_changed{};
    ImageIrisPtr prepared_iris{};
    bool iris_changed{};
    DenoisePolicy denoise_policy{};
    bool aovs_changed{};
    bool lightgroups_changed{};
    CryptomatteSettings crypto{};
    bool crypto_changed{};
    Clock::time_point preparation_started{};
    bool object_edits{};
    WorldData effective_world{};
    bool world_changed{};
    bool world_graph_changed{};
    ShaderGraphRequirements world_requirements{};
    ccl::unique_ptr<ccl::ShaderGraph> prepared_world{};
    std::vector<LightData> effective_lights{};
    std::map<std::string, ShaderGraphRequirements> light_requirements{};
    std::map<std::string, ccl::unique_ptr<ccl::ShaderGraph>> prepared_lights{};
    std::map<std::string, ccl::unique_ptr<ccl::ShaderGraph>> prepared_graphs{};
    std::map<std::string, ShaderGraphRequirements> requirements{};
    std::vector<MaterialData> effective_materials{};
    std::set<BindingKey> bindings_needed{};
    VolumeRecovery volume_recovery{};
    std::map<BindingKey, ShaderGraphRequirements> recovered_requirements{};
    std::map<BindingKey, ccl::unique_ptr<ccl::ShaderGraph>> prepared_bindings{};
    std::vector<ObjectRecord *> pose_records{};
    std::vector<ObjectRecord *> attribute_records{}; // Borrowed under control; no accepted mutation during prepare.
    std::uint64_t expanded_attribute_updates{};
    std::string attribute_fallback_reason;
    std::map<std::string, VolumeVectors> prepared_volume_vectors{};
    std::uint64_t prepared_vector_merges{};
    double volume_vector_seconds{};
    BindingHandoffs binding_handoffs{};
    std::map<std::string, Float3> volume_bounds{};
  };

  std::optional<SceneUpdate>
  expand_object_attributes(const SceneUpdate &incoming,
                           const std::vector<ObjectRecord *> *direct_records = nullptr) const;
  // Null slots expand individually; no optional value means global fallback.
  std::optional<std::vector<ObjectRecord *>> direct_attribute_records(const SceneUpdate &incoming,
                                                                      std::string &fallback_reason);
  std::optional<SceneUpdate> expand_displacement_materials(const SceneUpdate &incoming) const;

  bool validate_displacement_motion(const SceneUpdate &incoming, const SceneUpdate &u,
                                    const std::vector<ObjectRecord *> &pose_records);

  PreparedUpdate prepare_update(const SceneUpdate &incoming, const SceneUpdate &u, UpdateProfile &profile);

  struct EngineUpdatePlan {
    std::optional<LinkingPlan> prepared_linking{};
    bool world_surface_emission{};
    bool linking_plan_changed{};
    bool reset_denoising_history{};
    double graph_preparation{};
    bool metadata_dirty{};
    bool automatic_camera_refinement{};
    bool cancelled_camera_refinement{};
    Clock::time_point started{};
  };

  void publish_generation(const SceneUpdate &u, const PreparedUpdate &prepared, const EngineUpdatePlan &plan);

  void install_prepared_images(PreparedUpdate &prepared);

  void sync_object_poses(const SceneUpdate &u, const std::vector<ObjectRecord *> &pose_records);
  void sync_object_pose_values(ObjectRecord &record, const Matrix &matrix, const std::vector<Matrix> &motion,
                               const std::optional<Matrix> &nominal);
  void sync_attribute_values(ObjectRecord &record, const std::vector<UniformAttribute> &fields);
  void sync_object_attributes(const SceneUpdate &u, const std::vector<ObjectRecord *> &attribute_records);

  void sync_camera(const SceneUpdate &u, const PreparedUpdate &prepared, std::set<std::string> &variant_updates);

  void sync_world(const SceneUpdate &u, PreparedUpdate &prepared, bool linking_plan_changed);

  // Runs only after every old output/display callback has returned. Prepared
  // graphs are installed here, then scene ownership and generation publish
  // in their original order. The caller retains all candidate storage.
  struct RenderUpdateFlags {
    bool previous_vectors{}, uv_pass_changed{}, rendering_changed{};
    UpdateProfile *profile{}; // Borrowed for this synchronous transaction only.
  };
  RenderUpdateFlags configure_candidate_rendering(const SceneUpdate &, const PreparedUpdate &, UpdateProfile &);
  std::set<std::string> resolve_geometry_variants(const SceneUpdate &, const PreparedUpdate &, UpdateProfile &);
  void delete_objects(const SceneUpdate &);
  void delete_lights(const SceneUpdate &);
  std::vector<ccl::Shader *> sync_material_bindings(const SceneUpdate &, PreparedUpdate &, std::set<std::string> &);
  void sync_geometry(const SceneUpdate &, PreparedUpdate &);
  void sync_lights(PreparedUpdate &);
  std::vector<ccl::Shader *> stage_resource_retirement(const SceneUpdate &, const std::set<ccl::Shader *> &);
  void finish_geometry_sync(const SceneUpdate &, PreparedUpdate &, std::set<std::string> &, const RenderUpdateFlags &,
                            const std::vector<ccl::Shader *> &, const std::vector<ccl::Shader *> &);
  void mutate_engine(const SceneUpdate &u, PreparedUpdate &prepared, EngineUpdatePlan &plan, UpdateProfile &profile);

  template <class Transaction> void run_engine_transaction(Transaction &&transaction) {
    auto &i = *this;
    if (i.config.persistent_updates) {
      const auto boundary_started = Clock::now();
      i.session->synchronize_scene([&]() {
        i.scene_boundary_wait_seconds = seconds(boundary_started);
        try {
          transaction();
        } catch (const std::exception &e) {
          i.store->fail(e.what());
          i.session->progress.set_error(e.what());
          i.session->progress.set_cancel("Scene transaction failed");
          i.state = SessionState::Failed;
          throw;
        }
      });
    } else {
      i.scene_boundary_wait_seconds = 0;
      transaction();
    }
  }
};
} // namespace cycles_houdini
