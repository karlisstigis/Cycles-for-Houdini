// SPDX-License-Identifier: Apache-2.0
#include "geometry_selection.h"
#include <limits>
#include <algorithm>
#include <stdexcept>
#include <GA/GA_Iterator.h>
#include <GEO/GEO_PrimPart.h>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_PrimPolySoup.h>
#include <GEO/GEO_PrimNURBCurve.h>
#include <GEO/GEO_PrimRBezCurve.h>
#include <GEO/GEO_PrimVolume.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimPacked.h>
#include <GU/GU_PrimVDB.h>
#include <UT/UT_ParallelUtil.h>
#include <atomic>
#include <cstring>
#include <optional>
#include <cstdlib>
namespace cycles_houdini {
const char *geometry_kind_name(NativeGeometryKind kind) {
  switch (kind) {
  case NativeGeometryKind::Mesh:
    return "mesh";
  case NativeGeometryKind::Points:
    return "points";
  case NativeGeometryKind::Curves:
    return "curves";
  case NativeGeometryKind::Volume:
    return "volume";
  case NativeGeometryKind::Packed:
    return "packed";
  }
  throw std::invalid_argument("Unknown native geometry kind");
}
GA_Range NativeGeometrySelection::primitive_range(const GU_Detail &source) const {
  return full_detail ? source.getPrimitiveRange() : GA_Range(source.getPrimitiveMap(), primitives);
}
GA_Range NativeGeometrySelection::point_range(const GU_Detail &source) const {
  return full_detail ? source.getPointRange() : GA_Range(source.getPointMap(), points);
}
GA_Range NativeGeometrySelection::vertex_range(const GU_Detail &source) const {
  return full_detail ? source.getVertexRange()
                     : GA_Range(source, primitive_range(source), GA_ATTRIB_VERTEX, GA_Range::primitiveref{}, false);
}
const NativeGeometrySelection *NativeGeometryPartitions::find(NativeGeometryKind kind) const {
  const auto &result = selections[std::size_t(kind)];
  return result.active ? &result : nullptr;
}
namespace {
// A detail made only of closed polygons and polygon soups whose primitives
// reference every point is classified on all cores. Its serial result is one
// full-detail mesh selection; any other detail returns empty and takes the
// serial walk below (CYCLES_HOUDINI_SERIAL_CONVERSION=1 always does).
std::optional<NativeGeometryPartitions> homogeneous_mesh(const GU_Detail &source) {
  const auto primitives = std::size_t(source.getNumPrimitives()), points = std::size_t(source.getNumPoints());
  const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  if (primitives < 4096 || (serial && std::strcmp(serial, "1") == 0))
    return {};
  std::vector<unsigned char> referenced(points, 0);
  std::atomic<bool> other{false};
  UTparallelFor(UT_BlockedRange<std::size_t>(0, primitives, 4096), [&](const UT_BlockedRange<std::size_t> &range) {
    for (auto i = range.begin(); i < range.end() && !other.load(std::memory_order_relaxed); ++i) {
      const auto *primitive = source.getPrimitive(source.primitiveOffset(GA_Index(i)));
      const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive);
      if (poly ? !poly->isClosed() : !dynamic_cast<const GEO_PrimPolySoup *>(primitive)) {
        other.store(true, std::memory_order_relaxed);
        return;
      }
      for (exint vertex = 0; vertex < primitive->getVertexCount(); ++vertex) {
        const auto index = source.pointIndex(primitive->getPointOffset(vertex));
        if (index < 0 || std::size_t(index) >= points) {
          other.store(true, std::memory_order_relaxed); // The serial walk reports it.
          return;
        }
        std::atomic_ref<unsigned char>(referenced[std::size_t(index)]).store(1, std::memory_order_relaxed);
      }
    }
  });
  if (other.load() || std::find(referenced.begin(), referenced.end(), 0) != referenced.end())
    return {};
  NativeGeometryPartitions result;
  for (std::size_t i = 0; i < result.selections.size(); ++i)
    result.selections[i].kind = NativeGeometryKind(i);
  auto &mesh = result.selections[std::size_t(NativeGeometryKind::Mesh)];
  mesh.active = mesh.full_detail = true;
  result.count = 1;
  return result;
}
} // namespace
NativeGeometryPartitions partition_geometry(const GU_Detail &source) {
  if (source.getNumPoints() > std::numeric_limits<int>::max())
    throw std::invalid_argument("Too many native source points for the pinned engine interface");
  if (auto result = homogeneous_mesh(source))
    return std::move(*result);
  NativeGeometryPartitions result;
  for (std::size_t i = 0; i < result.selections.size(); ++i)
    result.selections[i].kind = NativeGeometryKind(i);
  std::vector<unsigned char> membership(std::size_t(source.getNumPoints()), 0);
  bool explicit_particles = false;
  for (GA_Iterator it(source.getPrimitiveRange()); !it.atEnd(); ++it) {
    const auto *primitive = source.getPrimitive(*it);
    NativeGeometryKind kind;
    if (dynamic_cast<const GU_PrimPacked *>(primitive))
      kind = NativeGeometryKind::Packed;
    else if (dynamic_cast<const GU_PrimVDB *>(primitive) || dynamic_cast<const GEO_PrimVolume *>(primitive))
      kind = NativeGeometryKind::Volume;
    else if (dynamic_cast<const GEO_PrimParticle *>(primitive)) {
      kind = NativeGeometryKind::Points;
      explicit_particles = true;
    } else if (const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive))
      kind = poly->isClosed() ? NativeGeometryKind::Mesh : NativeGeometryKind::Curves;
    else if (dynamic_cast<const GEO_PrimPolySoup *>(primitive))
      kind = NativeGeometryKind::Mesh;
    else if (dynamic_cast<const GEO_PrimNURBCurve *>(primitive) || dynamic_cast<const GEO_PrimRBezCurve *>(primitive))
      kind = NativeGeometryKind::Curves;
    else {
      ++result.unsupported_count;
      if (result.unsupported_examples.size() < 16)
        result.unsupported_examples.push_back(source.primitiveIndex(*it));
      // These are still referenced points, not a free particle cloud.
      for (exint vertex = 0; vertex < primitive->getVertexCount(); ++vertex) {
        const auto index = source.pointIndex(primitive->getPointOffset(vertex));
        if (index < 0 || index >= source.getNumPoints())
          throw std::invalid_argument("Native primitive references an invalid point");
        membership[std::size_t(index)] |= 128;
      }
      continue;
    }
    auto &selection = result.selections[std::size_t(kind)];
    selection.active = true;
    selection.primitives.append(*it);
    selection.parametric_curves |=
        kind == NativeGeometryKind::Curves && dynamic_cast<const GEO_Curve *>(primitive) != nullptr;
    if (kind == NativeGeometryKind::Curves)
      if (const auto *curve = dynamic_cast<const GEO_Curve *>(primitive))
        selection.rational_curves |= curve->isRational() != 0;
    const auto bit = static_cast<unsigned char>(1u << std::size_t(kind));
    for (exint vertex = 0; vertex < primitive->getVertexCount(); ++vertex) {
      const auto index = source.pointIndex(primitive->getPointOffset(vertex));
      if (index < 0 || index >= source.getNumPoints())
        throw std::invalid_argument("Native primitive references an invalid point");
      membership[std::size_t(index)] |= bit;
    }
  }
  for (GA_Iterator it(source.getPointRange()); !it.atEnd(); ++it) {
    const auto bits = membership[std::size_t(source.pointIndex(*it))];
    for (std::size_t kind = 0; kind < result.selections.size(); ++kind)
      if (bits & (1u << kind))
        result.selections[kind].points.append(*it);
    // Explicit particle primitives retain their existing membership policy:
    // points outside their vertex lists are not extra particles. Otherwise
    // only points unreferenced by every primitive form the loose point cloud.
    if (!bits && !explicit_particles) {
      auto &points = result.selections[std::size_t(NativeGeometryKind::Points)];
      points.active = true;
      points.points.append(*it);
    }
  }
  for (const auto &selection : result.selections)
    result.count += selection.active;
  if (!result.count) {
    result.selections[std::size_t(NativeGeometryKind::Points)].active = true;
    result.count = 1;
  }
  if (result.count == 1 && !result.unsupported_count)
    for (auto &selection : result.selections)
      if (selection.active) {
        selection.full_detail = true;
        selection.primitives.clear();
        selection.points.clear();
      }
  return result;
}
const NativeGeometryPartitions &NativePartitionCache::inspect(const GU_Detail &source) { return *acquire(source); }
std::shared_ptr<const NativeGeometryPartitions> NativePartitionCache::acquire(const GU_Detail &source) {
  std::vector<std::int64_t> ids{source.getTopology().getDataId(), source.getPrimitiveList().getDataId(),
                                source.getNumPrimitives(), source.getNumPoints()};
  auto &record = records_[source.getUniqueId()];
  const bool valid = ids[0] != GA_INVALID_DATAID && ids[1] != GA_INVALID_DATAID;
  if (valid && record.identifiers == ids) {
    ++hits_;
    return record.partitions;
  }
  const Layout layout{ids[0], ids[1], ids[2], ids[3]};
  // Cloned topology/list data IDs identify unchanged structure. Intern only
  // homogeneous full-detail selections: they contain no source-specific
  // offset lists or unsupported primitive indices to accidentally transplant.
  if (valid)
    if (const auto found = shared_layouts_.find(layout); found != shared_layouts_.end())
      if (auto shared = found->second.lock()) {
        ++hits_;
        record.identifiers = std::move(ids);
        record.partitions = std::move(shared);
        return record.partitions;
      }
  ++scans_;
  auto partitions = std::make_shared<const NativeGeometryPartitions>(partition_geometry(source));
  record.identifiers = std::move(ids);
  record.partitions = std::move(partitions);
  if (valid && record.partitions->count == 1 && !record.partitions->unsupported_count)
    for (const auto &selection : record.partitions->selections)
      if (selection.active && selection.full_detail) {
        shared_layouts_[layout] = record.partitions;
        break;
      }
  return record.partitions;
}
void NativePartitionCache::retire(const std::set<std::int64_t> &details) {
  for (auto it = records_.begin(); it != records_.end();)
    if (!details.contains(it->first))
      it = records_.erase(it);
    else
      ++it;
  std::erase_if(shared_layouts_, [](const auto &row) { return row.second.expired(); });
}
std::uint64_t NativePartitionCache::bytes() const {
  std::uint64_t result =
      shared_layouts_.size() * (sizeof(Layout) + sizeof(std::weak_ptr<const NativeGeometryPartitions>));
  std::set<const NativeGeometryPartitions *> counted;
  for (const auto &[id, record] : records_) {
    result += sizeof(record) + record.identifiers.capacity() * sizeof(std::int64_t);
    if (record.partitions && counted.insert(record.partitions.get()).second) {
      result += record.partitions->unsupported_examples.capacity() * sizeof(GA_Index);
      for (const auto &selection : record.partitions->selections)
        result += selection.primitives.getMemoryUsage(false) + selection.points.getMemoryUsage(false);
    }
  }
  return result;
}
} // namespace cycles_houdini
