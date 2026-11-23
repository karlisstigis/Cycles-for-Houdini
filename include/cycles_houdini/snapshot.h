// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <map>
#include <set>
#include <span>

namespace cycles_houdini {
inline constexpr std::uint32_t worker_protocol_version =
    92; // 92: SessionDiagnostics.device_update_memory (per-device per-update tracked memory record); 91:
        // WorkerUpdate.root_poses (gesture root moves: origins once, then root matrices); 90: Refit Meshes While
        // Interacting in SessionConfig; 89: reused input mapping with command 14 (release input view); 88: raw sparse
        // volume grids; 87: Curve Subdivisions in SessionConfig (86: no per-material mesh-media flag; 85: added it; 84:
        // light portals; 82: material settings).
using MeshPayloadSources = std::map<std::string, std::shared_ptr<const MeshData>>;
// Desired state is distinct from the last engine acknowledgement. Snapshots
// share immutable geometry and own every scalar/string/material definition.
struct SceneSnapshotState {
  std::optional<std::vector<SourceDiagnostic>> source_diagnostics;
  std::map<std::string, MaterialData> materials;
  std::map<std::string, std::shared_ptr<const MeshData>> meshes;
  std::map<std::string, std::shared_ptr<const VolumeData>> volumes;
  std::map<std::string, std::shared_ptr<const PointData>> points;
  std::map<std::string, std::shared_ptr<const CurveData>> curves;
  // Object tables are immutable once published. Camera/material-only states
  // retain this table; object edits construct a private replacement instead.
  using ObjectTable = std::map<std::string, ObjectData>;
  std::shared_ptr<const ObjectTable> objects{std::make_shared<const ObjectTable>()};
  std::map<std::string, LightData> lights;
  std::optional<DenoiseSettings> denoising;
  std::optional<std::vector<std::string>> passes;
  std::optional<std::vector<AovDefinition>> aovs;
  std::optional<std::vector<std::string>> lightgroups;
  std::optional<CryptomatteSettings> cryptomatte;
  std::optional<RenderSettings> rendering;
  std::optional<CameraData> camera;
  std::optional<DicingSettings> dicing;
  std::optional<Crop> crop;
  std::optional<Float3> background;
  std::optional<WorldData> world;
  std::optional<LinkingSettings> linking;
  std::optional<std::string> background_lightgroup;
  std::optional<bool> background_shadow_catcher;
  std::optional<bool> transparent_background, transparent_glass, motion_blur;
  std::optional<float> transparent_roughness_threshold;
  bool defer_camera_subdivision{};
};
std::shared_ptr<const SceneSnapshotState> snapshot_state(const std::shared_ptr<const SceneSnapshotState> &previous,
                                                         const SceneUpdate &update);
// As above, but the caller supplies the resulting immutable object table: it
// must equal what the update's object records would build from previous (the
// native adapter proves this, NativeSceneAdapter::snapshot_objects). The
// update's object records and deletions are then not applied again.
std::shared_ptr<const SceneSnapshotState>
snapshot_state(const std::shared_ptr<const SceneSnapshotState> &previous, const SceneUpdate &update,
               std::shared_ptr<const SceneSnapshotState::ObjectTable> objects);
SceneUpdate snapshot_difference(const SceneSnapshotState *acknowledged, const SceneSnapshotState &desired);

struct TexturePolicyEntry {
  TextureHeader header;
  TextureInputSelection selection;
};
struct OwnedTexturePolicy {
  std::string identity;
  std::vector<TexturePolicyEntry> entries;
  TextureImageTable images; // Full owned desired/accepted ledger; wire uses deltas below.
};
std::vector<TextureImageKey> texture_image_requests(const SceneSnapshotState &state);
// Host acquisition and worker validation use the same emitter/material ledger.
std::shared_ptr<const SceneSnapshotState>
shader_resource_state(const std::shared_ptr<const SceneSnapshotState> &previous, const SceneUpdate &update);
void validate_texture_image_references(const SceneSnapshotState &state, const OwnedTexturePolicy &policy);
TextureInputPolicy snapshot_texture_policy(const std::shared_ptr<const OwnedTexturePolicy> &policy);
class SnapshotTextureResolver {
public:
  explicit SnapshotTextureResolver(TextureInputPolicy host_policy);
  ~SnapshotTextureResolver();
  // Host thread only. Cache checks use file stamps; unchanged headers are not reopened.
  OwnedTexturePolicy capture(const SceneSnapshotState &state);

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
struct OwnedColorPolicy {
  // Houdini snapshots always use the resolved file. YAML is only the fallback
  // for in-memory configurations authored by the standalone C++ harness.
  std::string config_path, yaml, working_directory, render_policy_id;
  std::map<std::string, std::string> context;
};
OwnedColorPolicy snapshot_color_policy(const std::shared_ptr<const ColorConfiguration> &color);
void activate_snapshot_color_policy(const OwnedColorPolicy &policy); // Worker process only.

struct NativeNavigationState; // Host-only immutable origins; never serialized.
struct OwnedSceneSnapshot {
  std::shared_ptr<const SceneSnapshotState> state;
  std::shared_ptr<const OwnedTexturePolicy> textures;
  std::shared_ptr<const ColorConfiguration> color;
  OwnedColorPolicy portable_color;
  std::shared_ptr<const NativeNavigationState> native_navigation;
  // Root-pose previews (protocol 91) share the accepted object table: its
  // instance poses are those of baked_navigation's roots, and native_navigation
  // holds the moved roots. Null: the table matches native_navigation.
  std::shared_ptr<const NativeNavigationState> baked_navigation;
  // Host bookkeeping, set by encode_snapshot: the origins this snapshot's
  // worker holds (token) and for which roots. Never serialized.
  mutable std::shared_ptr<const void> worker_origin_token;
  mutable std::shared_ptr<const std::set<std::string>> worker_origin_roots;
  bool reuse_if_unchanged{};
};
struct WorkerConfiguration {
  SessionConfig session;
  OwnedColorPolicy color;
  std::string resource_root;
};
// Protocol 91, gesture pose path only. A root move travels as the root's
// matrices; the worker derives each instance pose from its origin, exactly as
// the host's per-instance path (python_snapshot.cpp navigation_snapshot):
// transform = centre x (local, or the middle motion origin), motion[i] =
// root.motion[i] x origin.motion[i], nominal = root.nominal x local when the
// object has a nominal transform. Origins arrive once per root and table.
struct PoseOriginRecord {
  std::string key, root;
  Matrix local{};
  std::vector<Matrix> motion;
  bool nominal{};
};
struct RootPoseRecord {
  std::string key;
  Matrix centre{}, nominal{};
  std::vector<Matrix> motion;
};
struct RootPoseUpdate {
  std::uint64_t token{};                 // Origins identity; 0 = no root update.
  bool reset_origins{};                  // Drop every origin the worker holds first.
  std::vector<PoseOriginRecord> origins; // Sorted by (root, key).
  std::vector<RootPoseRecord> roots;     // Sorted by key.
};
std::vector<ObjectPoseData> derive_root_poses(const std::map<std::string, std::vector<PoseOriginRecord>> &origins,
                                              const std::vector<RootPoseRecord> &roots);
struct WorkerUpdate {
  SceneUpdate scene;
  OwnedTexturePolicy textures;
  OwnedColorPolicy color;
  bool reuse_if_unchanged{};
  std::vector<std::shared_ptr<const TextureImageData>> image_upserts;
  std::vector<TextureImageKey> deleted_images;
  RootPoseUpdate root_poses;
};
void texture_image_difference(const OwnedTexturePolicy *previous, const OwnedTexturePolicy &desired,
                              WorkerUpdate &update);
std::shared_ptr<const OwnedTexturePolicy>
apply_texture_image_difference(const std::shared_ptr<const OwnedTexturePolicy> &previous, WorkerUpdate &update);
struct WorkerOutput {
  std::string filename;
  RenderSession::OutputOptions options;
};
std::vector<std::byte> encode_worker_output(const WorkerOutput &value);
WorkerOutput decode_worker_output(std::span<const std::byte> bytes);
std::vector<std::byte> encode_worker_preview(const PreviewRequest &value);
PreviewRequest decode_worker_preview(std::span<const std::byte> bytes);
struct WorkerReply {
  bool success{true};
  std::string error;
  bool update_rejected{}; // Only pre-transaction validation; not a device/transport failure.
  std::uint64_t renderer_pid{};
  SessionStatus status;
  SessionDiagnostics diagnostics;
  std::vector<TextureInputInfo> textures;
  bool textures_changed{};
  std::string process_memory;            // Opt-in command; observer cost excluded from edit timings.
  std::vector<DeviceDescriptor> devices; // Probed in the renderer process, including NGX runtime support.
  std::shared_ptr<const FrameEvent> frame;
  std::shared_ptr<const FrameEvent> focus_frame;
  bool focus_is_frame{}; // Alias the beauty reply instead of encoding it twice.
};
// Versioned, bounded little-endian wire format. Volume trees are serialized
// through OpenVDB streams; no pointers or callbacks are written to the wire.
std::vector<std::byte> encode_worker_configuration(const WorkerConfiguration &value);
WorkerConfiguration decode_worker_configuration(std::span<const std::byte> bytes);
// Optional references are confined to the last accepted scene on this channel.
// Stateless packets still support self-contained, earlier-entry payload reuse.
// The final switch is a same-runtime measurement control, not an IPR mode.
std::vector<std::byte> encode_worker_update(const WorkerUpdate &value, const MeshPayloadSources *acknowledged = nullptr,
                                            bool reuse_mesh_payloads = true);
WorkerUpdate decode_worker_update(std::span<const std::byte> bytes, const MeshPayloadSources *acknowledged = nullptr,
                                  std::uint64_t allocation_limit = 8ull * 1024 * 1024 * 1024);
// B1 transfer: exactly the bytes encode_worker_update returns, but arrays of at
// least 16 KiB (and volume leaf buffers) are not copied while encoding. They stay
// in the update, which the GatheredUpdate keeps alive and which must not change,
// until write() copies everything, on all cores, straight into the caller's
// storage (the reused input mapping). CYCLES_HOUDINI_SERIAL_CONVERSION=1 writes
// on the calling thread. Copies share the encoded result.
class GatheredUpdate {
public:
  struct Impl;
  GatheredUpdate();
  explicit GatheredUpdate(std::shared_ptr<const Impl> impl);
  std::uint64_t size() const noexcept;
  void write(std::byte *target) const; // Writes exactly size() bytes.

private:
  std::shared_ptr<const Impl> impl_;
};
GatheredUpdate encode_worker_update_gathered(std::shared_ptr<const WorkerUpdate> value,
                                             const MeshPayloadSources *acknowledged = nullptr,
                                             bool reuse_mesh_payloads = true);
MeshPayloadSources mesh_payload_sources(const MeshPayloadSources &previous, const SceneUpdate &accepted);
std::vector<std::byte> encode_worker_color(const OwnedColorPolicy &value);
OwnedColorPolicy decode_worker_color(std::span<const std::byte> bytes);
std::vector<std::byte> encode_worker_reply(const WorkerReply &value);
WorkerReply decode_worker_reply(std::span<const std::byte> bytes,
                                const std::shared_ptr<const ColorConfiguration> &color);
} // namespace cycles_houdini
