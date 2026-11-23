// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <cstdint>
#include <map>
#include <set>
#include <vector>
#include <GA/GA_OffsetList.h>
#include <GA/GA_Range.h>
#include <memory>
class GU_Detail;
namespace cycles_houdini {
enum class NativeGeometryKind { Mesh, Points, Curves, Volume, Packed };
const char *geometry_kind_name(NativeGeometryKind kind);
// Owned offset metadata only. Ranges borrow the currently retained host detail
// during conversion. Its owner must retain that immutable detail for the whole
// conversion, including when conversion runs on a background thread.
struct NativeGeometrySelection {
  NativeGeometryKind kind{NativeGeometryKind::Mesh};
  bool active{}, full_detail{};
  GA_OffsetList primitives, points;
  bool parametric_curves{};
  bool rational_curves{};
  GA_Range primitive_range(const GU_Detail &source) const;
  GA_Range point_range(const GU_Detail &source) const;
  GA_Range vertex_range(const GU_Detail &source) const;
};
struct NativeGeometryPartitions {
  std::size_t unsupported_count{};
  std::vector<GA_Index> unsupported_examples;
  std::array<NativeGeometrySelection, 5> selections;
  std::size_t count{};
  const NativeGeometrySelection *find(NativeGeometryKind kind) const;
};
NativeGeometryPartitions partition_geometry(const GU_Detail &source);
class NativePartitionCache {
public:
  const NativeGeometryPartitions &inspect(const GU_Detail &source);
  std::shared_ptr<const NativeGeometryPartitions> acquire(const GU_Detail &source);
  void clear() {
    records_.clear();
    shared_layouts_.clear();
  }
  void reset() {
    clear();
    scans_ = hits_ = 0;
  }
  void retire(const std::set<std::int64_t> &details);
  std::uint64_t scans() const { return scans_; }
  std::uint64_t hits() const { return hits_; }
  std::uint64_t bytes() const;

private:
  struct Record {
    std::vector<std::int64_t> identifiers;
    std::shared_ptr<const NativeGeometryPartitions> partitions;
  };
  std::map<std::int64_t, Record> records_;
  using Layout = std::array<std::int64_t, 4>;
  std::map<Layout, std::weak_ptr<const NativeGeometryPartitions>> shared_layouts_;
  std::uint64_t scans_{}, hits_{};
};
} // namespace cycles_houdini
