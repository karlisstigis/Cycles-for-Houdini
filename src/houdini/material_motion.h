// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene_adapter.h"
#include "packed_adapter.h"
class SOP_Node;

namespace cycles_houdini {
struct NativeMaterialRoute {
  std::size_t index{};
  std::string identity;
  bool indexed{};
};
struct NativeMaterialMotionProfile {
  std::shared_ptr<const NativeMaterialLookup> materials;
  std::string signature;
  std::vector<SourceDiagnostic> diagnostics;
};
// Pure owned shutter hierarchy. Unlike the host preparation context this
// object has no material resolver/callback and may expand poses on a converter.
class NativeMaterialMotionPoses {
public:
  struct Impl;
  explicit NativeMaterialMotionPoses(std::unique_ptr<Impl> impl);
  ~NativeMaterialMotionPoses();
  bool poses(std::size_t sample, const GeometryRequest &request, std::map<std::string, Matrix> &locals,
             std::set<std::string> &population, std::string &warning) const;
  // Structural routing identity only. A caller caching these tokens must
  // retain this owner; shader preparation still happens on the host.
  std::vector<std::uintptr_t> route_identity(const std::vector<NativeMaterialRoute> &route) const;

private:
  std::unique_ptr<Impl> impl_;
};
class NativeMaterialMotionContext {
public:
  struct Impl;
  explicit NativeMaterialMotionContext(std::unique_ptr<Impl> impl);
  ~NativeMaterialMotionContext();
  const std::shared_ptr<const PreservedDetail> &nominal() const;
  std::shared_ptr<const NativePackedDetails> packed_details() const;
  // Share only roots acquired in this capture. An older material-only cache
  // stamp cannot certify arbitrary fields whose off-frame expression changed
  // while nominal values/IDs remained identical. False requests reacquisition.
  bool sample_detail(std::size_t sample, std::shared_ptr<const PreservedDetail> &detail, std::string &warning) const;
  const std::set<std::string> &dependencies() const;
  const std::vector<std::string> &stylesheet_resources() const;
  std::shared_ptr<const NativeMaterialMotionProfile> profile(const std::vector<NativeMaterialRoute> &route);
  std::shared_ptr<const NativeMaterialMotionPoses> pose_source() const;

private:
  std::unique_ptr<Impl> impl_;
};
// Cache owns immutable nominal geometry and plain off-frame binding/transform
// metadata. Host callbacks live only in the transient context, never this cache.
class NativeMaterialMotionCache {
public:
  NativeMaterialMotionCache();
  ~NativeMaterialMotionCache();
  std::shared_ptr<NativeMaterialMotionContext>
  prepare(NativeRootSource &sop, const GeometryRequest &request, const NativeMotionWindow &window,
          const std::shared_ptr<const PreservedDetail> &nominal,
          const std::shared_ptr<const NativePackedDetails> &frozen, const NativeMaterialPreparation &materials,
          ExtractionStats &stats, NativeMaterialBindingCache &bindings, NativePartitionCache &partitions,
          NativeStyleCache &styles, NativeStyleBindingCache &style_bindings, NativePackedPreparationCache &packed,
          NativePreparationBudget *budget = nullptr, std::map<std::string, double> *timings = nullptr);
  void release_capture_work();
  void retire(const std::set<std::string> &roots);

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace cycles_houdini
