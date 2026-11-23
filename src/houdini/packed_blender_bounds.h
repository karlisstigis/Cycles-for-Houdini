// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <atomic>
#include <memory>
#include <string>

namespace cycles_houdini {
struct BlenderReferenceBounds {
  std::array<double, 6> box{};
  std::string source_identity;
  std::string serialized;
};
struct BlenderReferenceBoundsLease {
  BlenderReferenceBounds bounds;
  std::atomic_bool valid{true};
};
struct BlenderReferencePreviewLease {
  std::string source_identity;
  std::atomic_bool valid{true};
};
// Bounded shared invalidation guards for saved, not-yet-validated viewport data.
std::shared_ptr<const BlenderReferencePreviewLease> lease_blender_reference_preview(const std::string &, double,
                                                                                    const std::string &);
bool blender_reference_preview_valid(const std::string &, double, const std::string &);
void invalidate_blender_reference_preview(const std::string &, double, const std::string &);
void restore_blender_reference_preview(const std::string &, double, const std::string &);
std::shared_ptr<const BlenderReferenceBoundsLease> lease_blender_reference_bounds(const std::string &, double);
// Table lookup only: no filesystem, Python or Blender calls from draw/bounds.
bool lookup_blender_reference_bounds(const std::string &, double, BlenderReferenceBounds &);
// Explicit render/artist publication captures native dependency signatures.
// Unknown records remove an old cache entry. Throws for malformed records.
void publish_blender_reference_bounds(const std::string &, double, const std::string &);
// Pure bounded JSON parsing for node/primitive persistence. No filesystem access.
bool preview_blender_reference_bounds(const std::string &, double, const std::string &, BlenderReferenceBounds &);
bool select_saved_blender_reference_bounds(const std::string &, const std::string &, double, BlenderReferenceBounds &);
std::string serialized_blender_reference_bounds(const std::string &, double);
// Validate a saved native payload only on an explicit validation operation.
bool restore_blender_reference_bounds(const std::string &, double, const std::string &, BlenderReferenceBounds &);
// Explicit cook checks invalidate the shared entry on an external source edit.
bool validate_blender_reference_bounds(const std::string &, double, BlenderReferenceBounds &);
void clear_blender_reference_bounds();
} // namespace cycles_houdini
