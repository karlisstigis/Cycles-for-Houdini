// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <openvdb/openvdb.h>
#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace cycles_houdini {
// Internal in-process resource. The adapter owns immutable grid headers and
// tree references; supported host writes use copy-on-write. Consumers must not
// mutate them. Worker transport serializes grids, never pointers or C++ objects.
struct VolumeField {
  std::string name;
  openvdb::GridBase::ConstPtr grid;
  // Producer-owned immutable field version, unrelated to SOP id/name values.
  // Zero is valid for low-level callers and uses conservative pointer identity.
  std::uint64_t revision{};
};
struct VolumeData {
  std::string key, material;
  std::uint64_t revision{};
  std::vector<VolumeField> fields;
  float step_size{};           // Zero uses the engine's grid-derived step size.
  bool object_space_density{}; // False: coefficients per world-space length.
  // Native source translation may recover unavailable shader fields locally.
  // Low-level engine fixtures retain strict validation by default.
  bool recover_material{};
  // Velocities are in geometry/object units per second, based at the nominal
  // SOP snapshot. Ray time is normalized over this shutter, not in seconds.
  float velocity_seconds{};            // Zero holds fields static.
  float velocity_reference_time{0.5f}; // Nominal time within normalized shutter.
};
// Shared native-recovery and strict-engine checks. Empty means usable; this
// inspects headers only, never scans or copies voxel trees. Velocity aliases
// are classified using their canonical names before normalization.
std::string volume_field_error(const VolumeField &field);
// A vector may be represented by three scalar fields named <name>.x/.y/.z.
// Resolve headers only. Explicit fields win, and missing/type/map conflicts
// remain local shader-binding issues for native sources, never guessed values.
struct VolumeVectorSources {
  std::array<openvdb::FloatGrid::ConstPtr, 3> components;
  std::string issue;
  std::array<std::uint64_t, 3> revisions{};
  bool usable() const { return issue.empty() && components[0] && components[1] && components[2]; }
};
VolumeVectorSources volume_vector_sources(const VolumeData &data, const std::string &name);
std::string volume_shader_field_error(const VolumeData &data, const std::string &name, bool scalar = false,
                                      bool vector = false);
// Pure sparse assembly, preserving inactive values and active topology. Call
// only for a demanded shader field, outside host capture and engine mutation.
openvdb::Vec3SGrid::ConstPtr merge_volume_vector(const VolumeVectorSources &sources, const std::string &name);
// Canonicalize Houdini field aliases and merge matching scalar velocity grids
// with sparse tree traversal (including tiles). Reject ambiguous sources.
void normalize_volume_fields(VolumeData &data);
// Validate velocity values and return a conservative object-space envelope.
// Called for new/changed owned volumes, never for a camera-only edit.
std::array<float, 3> volume_velocity_bounds(const VolumeData &data);
} // namespace cycles_houdini
