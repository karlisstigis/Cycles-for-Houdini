// SPDX-License-Identifier: Apache-2.0
#include "mesh_adapter.h"
#include "identity_attribute.h"
#include "shading_attribute.h"
#include "named_attribute.h"
#include "../core/mesh_pointiness.h"
#include <limits>
#include <stdexcept>
#include <unordered_set>
#include <bit>
#include <map>
#include <set>
#include <cmath>
#include <algorithm>
#include <GA/GA_ElementGroup.h>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GEO/GEO_Convex.h>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_PrimPolySoup.h>
#include <GU/GU_Detail.h>
#include <UT/UT_ParallelUtil.h>
#include <atomic>
#include <cstring>
#include <exception>
#include <xmmintrin.h>
#include <cstdlib>

namespace cycles_houdini {
namespace {

Float3 value(const UT_Vector3 &v) { return {float(v.x()), float(v.y()), float(v.z())}; }
// Per-element mesh work on Houdini's TBB pool, with the packed converter's
// contract (scene_adapter.cpp parallel_slots): each task writes only its own
// slots and runs with the caller's SSE control word, so every value is bitwise
// the serial one. CYCLES_HOUDINI_SERIAL_CONVERSION=1 runs it on this thread.
class MeshFloatControl {
public:
  explicit MeshFloatControl(unsigned int control) : saved_(_mm_getcsr()) { _mm_setcsr(control); }
  ~MeshFloatControl() { _mm_setcsr(saved_); }
  MeshFloatControl(const MeshFloatControl &) = delete;
  MeshFloatControl &operator=(const MeshFloatControl &) = delete;

private:
  unsigned int saved_;
};
bool serial_mesh_conversion() {
  const auto *value = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  return value && std::strcmp(value, "1") == 0;
}
template <class Body> void mesh_slots(std::size_t count, std::size_t grain, const Body &body) {
  if (count <= grain || serial_mesh_conversion()) {
    body(std::size_t{0}, count);
    return;
  }
  const auto control = _mm_getcsr();
  UTparallelFor(UT_BlockedRange<std::size_t>(0, count, grain), [&](const UT_BlockedRange<std::size_t> &range) {
    MeshFloatControl scope(control);
    body(range.begin(), range.end());
  });
}
// Lowest failing slot across tasks; the minimum does not depend on the schedule.
void first_failure(std::atomic<std::uint64_t> &first, std::uint64_t slot, int code) {
  const std::uint64_t packed = slot << 2 | std::uint64_t(code);
  auto current = first.load(std::memory_order_relaxed);
  while (packed < current && !first.compare_exchange_weak(current, packed, std::memory_order_relaxed)) {
  }
}
void numeric(const GA_Attribute *attribute, const char *name, int count) {
  if (attribute && (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != count))
    throw std::invalid_argument(std::string("Mesh ") + name + " must be a " + std::to_string(count) +
                                "-component float attribute");
}
int bounded(std::size_t count) {
  if (count > std::size_t(std::numeric_limits<int>::max()))
    throw std::invalid_argument("Mesh provenance exceeds the pinned integer interface");
  return int(count);
}
// Diagnose authored control topology before it reaches the strict owned-engine
// contract. A polygon render can remain useful when subdivision is unavailable.
std::string subdivision_topology_error(const NativeMeshTopology &top) {
  struct Edge {
    int count{}, orientation{};
  };
  std::map<std::pair<int, int>, Edge> edges;
  for (const auto &face : top.faces) {
    std::set<int> points;
    for (int c = 0; c < face.corner_count; ++c) {
      const int a = top.corner_points[face.first_corner + c],
                b = top.corner_points[face.first_corner + (c + 1) % face.corner_count];
      if (!points.insert(a).second)
        return "Primitive " + std::to_string(face.primitive) + " repeats a point within a control face";
      const auto pair = std::minmax(a, b);
      auto &edge = edges[{pair.first, pair.second}];
      ++edge.count;
      edge.orientation += a < b ? 1 : -1;
      if (edge.count > 2)
        return "A control-mesh edge belongs to more than two polygon faces";
      if (edge.count == 2 && edge.orientation)
        return "Control faces have inconsistent shared-edge winding";
    }
  }
  return {};
}
std::vector<std::int64_t> attributes(const GA_Attribute *a, const GA_Attribute *b) {
  std::vector<std::int64_t> ids;
  for (const auto *attribute : {a, b}) {
    ids.push_back(attribute != nullptr);
    if (attribute)
      ids.insert(ids.end(), {attribute->getDataId(), attribute->getStorageClass(), attribute->getTupleSize()});
  }
  return ids;
}
bool valid_attributes(const GA_Attribute *a, const GA_Attribute *b) {
  return (!a || a->getDataId() != GA_INVALID_DATAID) && (!b || b->getDataId() != GA_INVALID_DATAID);
}
template <class Callback> void polygon(const GU_Detail &source, const NativeMeshFace &face, Callback &&callback) {
  const auto *primitive = source.getPrimitive(source.primitiveOffset(face.primitive));
  if (const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive)) {
    if (!poly->isClosed())
      throw std::invalid_argument("Native mesh contains an open polygon");
    callback(*poly);
  } else if (const auto *soup = dynamic_cast<const GEO_PrimPolySoup *>(primitive)) {
    GEO_PrimPolySoup::PolygonIterator part(*soup, face.polygon);
    if (part.atEnd())
      throw std::invalid_argument("Polygon soup provenance is no longer valid");
    callback(part);
  } else
    throw std::invalid_argument("Native mesh requires closed polygons or polygon soups");
}
void triangulate(const GU_Detail &source, NativeMeshFace &face, std::vector<Triangle> &result,
                 UT_Array<GEO_TriOrQuad> &scratch) {
  face.first_triangle = bounded(result.size());
  polygon(source, face, [&](const auto &poly) {
    if (poly.getFastVertexCount() < 3)
      throw std::invalid_argument("Mesh polygon has fewer than three corners");
    if (!GEOtriangulatePoly(poly, scratch))
      result.push_back({face.first_corner, face.first_corner + 1, face.first_corner + 2});
    else
      for (const auto &triangle : scratch) {
        if (!triangle.isTri())
          throw std::runtime_error("Houdini triangulation returned a quad");
        result.push_back({face.first_corner + int(triangle.vertices[0]), face.first_corner + int(triangle.vertices[1]),
                          face.first_corner + int(triangle.vertices[2])});
      }
  });
  face.triangle_count = bounded(result.size()) - face.first_triangle;
}
// Parallel form of topology_matches for a whole detail of polygons, where face
// i is primitive i. Empty when the detail holds polygon soups (their faces do
// not follow primitive numbers); the serial comparison then decides.
std::optional<bool> polygon_topology_matches(const GU_Detail &source, const NativeMeshTopology &previous) {
  const auto primitives = std::size_t(source.getNumPrimitives()), points = std::size_t(source.getNumPoints());
  if (previous.faces.size() != primitives || previous.source_points.size() != points)
    return {};
  // The serial walk visits points first: a point difference decides alone.
  std::atomic<bool> moved_points{false};
  mesh_slots(points, 16384, [&](std::size_t begin, std::size_t end) {
    for (auto i = begin; i < end; ++i)
      if (previous.source_points[i] != int(i)) {
        moved_points.store(true, std::memory_order_relaxed);
        return;
      }
  });
  if (moved_points.load())
    return false;
  // Bit 1: a face differs. Bit 2: a polygon soup, so faces need not follow
  // primitive numbers; every primitive is visited until one is found.
  std::atomic<int> state{0};
  mesh_slots(primitives, 4096, [&](std::size_t begin, std::size_t end) {
    for (auto i = begin; i < end && !(state.load(std::memory_order_relaxed) & 2); ++i) {
      const auto offset = source.primitiveOffset(GA_Index(i));
      const auto *primitive = source.getPrimitive(offset);
      const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive);
      if (!poly) {
        state.fetch_or(dynamic_cast<const GEO_PrimPolySoup *>(primitive) ? 2 : 1, std::memory_order_relaxed);
        continue;
      }
      if (state.load(std::memory_order_relaxed) & 1)
        continue;
      const auto &face = previous.faces[i];
      bool same = poly->isClosed() && face.primitive == int(i) && face.polygon == 0 &&
                  face.corner_count == poly->getFastVertexCount();
      for (int c = 0; same && c < face.corner_count; ++c) {
        const auto corner = face.first_corner + c;
        same = source.vertexIndex(poly->getFastVertexOffset(c)) == previous.corner_vertices[corner] &&
               source.pointIndex(poly->getPointOffset(c)) == previous.source_points[previous.corner_points[corner]];
      }
      if (!same)
        state.fetch_or(1, std::memory_order_relaxed);
    }
  });
  const auto result = state.load();
  if (result & 2)
    return {};
  return result == 0;
}
bool topology_matches(const GU_Detail &source, const NativeGeometrySelection *selection,
                      const NativeMeshTopology &previous) {
  // A cook may replace conservative data IDs without changing connectivity.
  // Compare exact logical point/vertex/face provenance before retaining it.
  if (!selection || selection->full_detail)
    if (const auto matched = polygon_topology_matches(source, previous))
      return *matched;
  const auto points = selection ? selection->point_range(source) : source.getPointRange();
  std::size_t point = 0;
  for (GA_Iterator it(points); !it.atEnd(); ++it, ++point)
    if (point >= previous.source_points.size() || source.pointIndex(*it) != previous.source_points[point])
      return false;
  if (point != previous.source_points.size())
    return false;
  std::size_t face_index = 0;
  const auto matches = [&](GA_Offset primitive, int part, const auto &poly) {
    if (face_index >= previous.faces.size())
      return false;
    const auto &face = previous.faces[face_index++];
    if (face.primitive != source.primitiveIndex(primitive) || face.polygon != part ||
        face.corner_count != poly.getFastVertexCount())
      return false;
    for (int c = 0; c < face.corner_count; ++c) {
      const auto corner = face.first_corner + c;
      if (source.vertexIndex(poly.getFastVertexOffset(c)) != previous.corner_vertices[corner] ||
          source.pointIndex(poly.getPointOffset(c)) != previous.source_points[previous.corner_points[corner]])
        return false;
    }
    return true;
  };
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  for (GA_Iterator it(primitives); !it.atEnd(); ++it) {
    const auto *primitive = source.getPrimitive(*it);
    if (const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive)) {
      if (!poly->isClosed() || !matches(*it, 0, *poly))
        return false;
    } else if (const auto *soup = dynamic_cast<const GEO_PrimPolySoup *>(primitive)) {
      int index = 0;
      for (GEO_PrimPolySoup::PolygonIterator part(*soup); !part.atEnd(); ++part, ++index)
        if (!matches(*it, index, part))
          return false;
    } else
      return false;
  }
  return face_index == previous.faces.size();
}
// Parallel form of topology() for a whole detail of closed polygons, where
// face i is primitive i and point i is source point i. Empty when the detail
// needs the serial walk (polygon soups, open or short polygons, any other
// primitive, a failing triangulation, integer overflow): that walk then
// builds the same topology or reports the same error.
std::shared_ptr<NativeMeshTopology> polygon_topology(const GU_Detail &source, bool subdivided,
                                                     NativeMeshCaptureMetrics &metrics) {
  const auto primitives = std::size_t(source.getNumPrimitives()), points = std::size_t(source.getNumPoints());
  if (primitives < 4096 || serial_mesh_conversion())
    return nullptr;
  std::vector<std::int64_t> first_corner(primitives + 1, 0);
  std::atomic<bool> other{false};
  mesh_slots(primitives, 4096, [&](std::size_t begin, std::size_t end) {
    for (auto i = begin; i < end && !other.load(std::memory_order_relaxed); ++i) {
      const auto *poly = dynamic_cast<const GEO_PrimPoly *>(source.getPrimitive(source.primitiveOffset(GA_Index(i))));
      if (!poly || !poly->isClosed() || poly->getFastVertexCount() < 3) {
        other.store(true, std::memory_order_relaxed);
        return;
      }
      first_corner[i + 1] = poly->getFastVertexCount();
    }
  });
  if (other.load())
    return nullptr;
  for (std::size_t i = 0; i < primitives; ++i)
    first_corner[i + 1] += first_corner[i];
  if (first_corner.back() > std::numeric_limits<int>::max())
    return nullptr;
  auto result = std::make_shared<NativeMeshTopology>();
  result->source_points.resize(points);
  for (std::size_t i = 0; i < points; ++i)
    result->source_points[i] = int(i);
  result->corner_points.resize(std::size_t(first_corner.back()));
  result->corner_vertices.resize(std::size_t(first_corner.back()));
  result->faces.resize(primitives);
  // Fixed chunks of faces own their triangles; a prefix over the chunks then
  // places them in face order, as the serial walk appends them.
  constexpr std::size_t chunk = 4096;
  const auto chunks = (primitives + chunk - 1) / chunk;
  std::vector<std::vector<Triangle>> triangles(subdivided ? 0 : chunks);
  NativeMeshFace *faces = result->faces.data();
  int *corner_points = result->corner_points.data();
  std::int64_t *corner_vertices = result->corner_vertices.data();
  mesh_slots(chunks, 1, [&](std::size_t begin, std::size_t end) {
    UT_Array<GEO_TriOrQuad> scratch;
    for (auto k = begin; k < end && !other.load(std::memory_order_relaxed); ++k)
      for (auto i = k * chunk; i < std::min(primitives, (k + 1) * chunk); ++i) {
        const auto *poly = static_cast<const GEO_PrimPoly *>(source.getPrimitive(source.primitiveOffset(GA_Index(i))));
        auto &face = faces[i];
        face.primitive = int(i);
        face.polygon = 0;
        face.first_corner = int(first_corner[i]);
        face.corner_count = int(first_corner[i + 1] - first_corner[i]);
        for (int corner = 0; corner < face.corner_count; ++corner) {
          corner_points[face.first_corner + corner] = int(source.pointIndex(poly->getPointOffset(corner)));
          corner_vertices[face.first_corner + corner] = source.vertexIndex(poly->getFastVertexOffset(corner));
        }
        if (subdivided)
          continue;
        try {
          triangulate(source, face, triangles[k], scratch);
        } catch (...) {
          other.store(true, std::memory_order_relaxed);
          return;
        }
      }
  });
  if (other.load())
    return nullptr;
  if (!subdivided) {
    std::vector<std::size_t> base(chunks + 1, 0);
    for (std::size_t k = 0; k < chunks; ++k)
      base[k + 1] = base[k] + triangles[k].size();
    if (base.back() > std::size_t(std::numeric_limits<int>::max()))
      return nullptr;
    result->triangle_corners.resize(base.back());
    Triangle *out = result->triangle_corners.data();
    mesh_slots(chunks, 1, [&](std::size_t begin, std::size_t end) {
      for (auto k = begin; k < end; ++k) {
        std::copy(triangles[k].begin(), triangles[k].end(), out + base[k]);
        for (auto i = k * chunk; i < std::min(primitives, (k + 1) * chunk); ++i)
          faces[i].first_triangle += int(base[k]);
      }
    });
    metrics.triangulated_faces += primitives;
  }
  ++metrics.topology_builds;
  return result;
}
std::shared_ptr<NativeMeshTopology> topology(const GU_Detail &source, const NativeGeometrySelection *selection,
                                             bool subdivided, NativeMeshCaptureMetrics &metrics) {
  if (!selection || selection->full_detail)
    if (auto result = polygon_topology(source, subdivided, metrics))
      return result;
  auto result = std::make_shared<NativeMeshTopology>();
  const auto points = selection ? selection->point_range(source) : source.getPointRange();
  std::vector<int> remap(std::size_t(source.getNumPoints()), -1);
  for (GA_Iterator it(points); !it.atEnd(); ++it) {
    const auto index = source.pointIndex(*it);
    remap.at(std::size_t(index)) = bounded(result->source_points.size());
    result->source_points.push_back(int(index));
  }
  const auto primitives = selection ? selection->primitive_range(source) : source.getPrimitiveRange();
  result->faces.reserve(primitives.getEntries());
  UT_Array<GEO_TriOrQuad> scratch;
  const auto add = [&](GA_Offset primitive, int part, const auto &poly) {
    NativeMeshFace face;
    face.primitive = int(source.primitiveIndex(primitive));
    face.polygon = part;
    face.first_corner = bounded(result->corner_points.size());
    face.corner_count = bounded(std::size_t(poly.getFastVertexCount()));
    if (face.corner_count < 3)
      throw std::invalid_argument("Mesh polygon has fewer than three corners");
    for (int corner = 0; corner < face.corner_count; ++corner) {
      const auto point = remap.at(std::size_t(source.pointIndex(poly.getPointOffset(corner))));
      if (point < 0)
        throw std::runtime_error("Mesh point partition lost polygon membership");
      result->corner_points.push_back(point);
      result->corner_vertices.push_back(source.vertexIndex(poly.getFastVertexOffset(corner)));
    }
    if (!subdivided) {
      triangulate(source, face, result->triangle_corners, scratch);
      ++metrics.triangulated_faces;
    }
    result->faces.push_back(face);
  };
  for (GA_Iterator it(primitives); !it.atEnd(); ++it) {
    const auto *primitive = source.getPrimitive(*it);
    if (const auto *poly = dynamic_cast<const GEO_PrimPoly *>(primitive)) {
      if (!poly->isClosed())
        throw std::invalid_argument("Native mesh contains an open polygon");
      add(*it, 0, *poly);
    } else if (const auto *soup = dynamic_cast<const GEO_PrimPolySoup *>(primitive)) {
      int index = 0;
      for (GEO_PrimPolySoup::PolygonIterator part(*soup); !part.atEnd(); ++part, ++index)
        add(*it, index, part);
    } else
      throw std::invalid_argument("Native mesh requires closed polygons or polygon soups");
  }
  bounded(result->corner_points.size());
  bounded(result->triangle_corners.size());
  ++metrics.topology_builds;
  return result;
}
} // namespace
void subdivision_identifiers(std::vector<std::int64_t> &values, const SubdivisionSettings &settings) {
  values.insert(values.end(),
                {std::int64_t(settings.type), std::int64_t(settings.space), std::int64_t(settings.boundary),
                 std::int64_t(settings.fvar), std::bit_cast<std::int32_t>(settings.dicing_rate), settings.max_level,
                 std::int64_t(settings.triangle_limit), settings.adaptive, settings.use_creases,
                 settings.cache_topology});
}
std::string subdivision_signature(const SubdivisionSettings &settings) {
  if (settings.type == SubdivisionType::None)
    return {};
  std::vector<std::int64_t> values;
  subdivision_identifiers(values, settings);
  std::string result = "/subdivision";
  for (const auto value : values)
    result += ":" + std::to_string(value);
  return result;
}
std::uint64_t mesh_capture_cache_bytes(const NativeMeshCaptureCache &cache, bool include_topology) {
  const auto &top = *cache.topology;
  return sizeof(cache) +
         (cache.topology_ids.capacity() + cache.normal_ids.capacity() + cache.uv_ids.capacity() +
          cache.color_ids.capacity()) *
             sizeof(std::int64_t) +
         (cache.named ? cache.named->bytes() : 0) + cache.subdivision_topology_error.size() +
         (include_topology
              ? sizeof(top) + top.source_points.capacity() * sizeof(int) + top.corner_points.capacity() * sizeof(int) +
                    top.corner_vertices.capacity() * sizeof(std::int64_t) +
                    top.faces.capacity() * sizeof(NativeMeshFace) + top.triangle_corners.capacity() * sizeof(Triangle)
              : 0);
}
MeshData capture_mesh(const GU_Detail &source, const GeometryRequest &request, std::uint64_t revision,
                      std::vector<Float3> *velocities, NativePointIdentities *identities, bool shading_attributes,
                      std::vector<NativeMaterialReference> *material_references,
                      const NativeGeometrySelection *selection, std::shared_ptr<NativeMeshCaptureCache> *cache,
                      NativeMeshCaptureMetrics *measured, std::vector<SourceDiagnostic> *diagnostics,
                      std::vector<Float3> *accelerations) {
  // Non-modifying public triangulation reads only the retained host detail.
  // Provenance and output buffers are owned before asynchronous use.
  NativeMeshCaptureMetrics local;
  auto &metrics = measured ? *measured : local;
  bool subdivided = request.subdivision.type != SubdivisionType::None;
  if (source.getNumPoints() > std::numeric_limits<int>::max() ||
      source.getNumPrimitives() > std::numeric_limits<int>::max())
    throw std::invalid_argument("Mesh exceeds the pinned Cycles integer interface");
  std::vector<std::int64_t> ids = {source.getTopology().getDataId(), source.getPrimitiveList().getDataId(),
                                   source.getNumPoints(), source.getNumPrimitives(), source.getNumVertices()};
  const auto previous = cache ? *cache : nullptr;
  bool reuse = false;
  if (previous && previous->data && previous->subdivision == request.subdivision &&
      std::equal(ids.begin() + 2, ids.end(), previous->topology_ids.begin() + 2)) {
    reuse = ids[0] != GA_INVALID_DATAID && ids[1] != GA_INVALID_DATAID && previous->topology_ids == ids;
    if (!reuse) {
      ++metrics.topology_scans;
      reuse = topology_matches(source, selection, *previous->topology);
    }
  }
  std::shared_ptr<const NativeMeshTopology> top;
  if (reuse) {
    top = previous->topology;
    ++metrics.topology_reuses;
  } else
    top = topology(source, selection, subdivided, metrics);
  std::string subdivision_error;
  if (subdivided) {
    subdivision_error = reuse ? previous->subdivision_topology_error : subdivision_topology_error(*top);
    if (!subdivision_error.empty()) {
      subdivided = false;
      if (!reuse)
        top = topology(source, selection, false, metrics);
      if (diagnostics)
        diagnostics->push_back(
            {"geometry.subdivision_topology", request.sop_path, request.geometry_key, subdivision_error,
             "Disabled subdivision for this mesh; retaining triangulated polygons, materials and other geometry"});
    }
  }
  MeshData data;
  data.key = request.geometry_key;
  data.revision = revision;
  data.smooth = request.smooth;
  data.motion_blur = request.motion_blur;
  const auto position_id = source.getP()->getDataId();
  const bool reuse_positions = reuse && position_id != GA_INVALID_DATAID && previous->position_id == position_id;
  std::vector<unsigned char> moved;
  if (reuse_positions)
    data.positions = previous->data->positions;
  else {
    const auto count = top->source_points.size();
    if (reuse && previous->data->positions.size() < count)
      throw std::out_of_range("Retained mesh positions do not match the retained topology");
    data.positions.resize(count);
    moved.resize(count);
    // Raw pointers: tasks must not reload vector members through shared objects.
    const int *points = top->source_points.data();
    Float3 *positions = data.positions.data();
    const Float3 *before = reuse ? previous->data->positions.data() : nullptr;
    unsigned char *changed = moved.data();
    mesh_slots(count, 16384, [&source, points, positions, before, changed](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i) {
        const auto p = value(source.getPos3(source.pointOffset(points[i])));
        positions[i] = p;
        if (before)
          changed[i] = p != before[i];
      }
    });
  }
  bool mapping_changed = !reuse;
  if (reuse_positions && !subdivided)
    metrics.retained_faces += top->faces.size();
  if (reuse && !reuse_positions && !subdivided) {
    // Unchanged topology alone does not guarantee unchanged triangulation.
    // Recompute polygons incident to moved points, retaining stationary faces.
    // Faces are triangulated on all cores into per-face outcomes; the
    // in-order pass below then makes exactly the serial walk's decisions,
    // counters and errors (a failing face is triangulated again to throw).
    enum : unsigned char { Retained, Same, Different, Recount, Failed };
    std::vector<unsigned char> outcome(top->faces.size(), Retained);
    mesh_slots(top->faces.size(), 2048, [&](std::size_t begin, std::size_t end) {
      UT_Array<GEO_TriOrQuad> scratch;
      std::vector<Triangle> changed;
      for (auto index = begin; index < end; ++index) {
        const auto &face = top->faces[index];
        bool dirty = false;
        if (face.corner_count > 3)
          for (int c = 0; c < face.corner_count && !dirty; ++c)
            dirty = moved[top->corner_points[face.first_corner + c]];
        if (!dirty)
          continue;
        changed.clear();
        auto updated = face;
        try {
          triangulate(source, updated, changed, scratch);
        } catch (...) {
          outcome[index] = Failed;
          continue;
        }
        if (updated.triangle_count != face.triangle_count) {
          outcome[index] = Recount;
          continue;
        }
        bool different = false;
        for (int i = 0; i < face.triangle_count; ++i) {
          const auto &a = changed[i];
          const auto &b = top->triangle_corners[face.first_triangle + i];
          different |= a.a != b.a || a.b != b.b || a.c != b.c;
        }
        outcome[index] = different ? Different : Same;
      }
    });
    std::shared_ptr<NativeMeshTopology> edited;
    UT_Array<GEO_TriOrQuad> scratch;
    std::vector<Triangle> changed;
    bool rebuild = false;
    for (std::size_t index = 0; index < top->faces.size() && !rebuild; ++index) {
      const auto &face = top->faces[index];
      switch (outcome[index]) {
      case Retained:
        ++metrics.retained_faces;
        continue;
      case Failed: {
        changed.clear();
        auto updated = face;
        triangulate(source, updated, changed, scratch);
        throw std::logic_error("Mesh triangulation failed on another thread only");
      }
      case Recount:
        ++metrics.triangulated_faces;
        rebuild = true;
        continue;
      case Different: {
        ++metrics.triangulated_faces;
        changed.clear();
        auto updated = face;
        triangulate(source, updated, changed, scratch);
        if (updated.triangle_count != face.triangle_count)
          throw std::logic_error("Mesh triangulation is not repeatable");
        if (!edited)
          edited = std::make_shared<NativeMeshTopology>(*top);
        std::copy(changed.begin(), changed.end(), edited->triangle_corners.begin() + face.first_triangle);
        mapping_changed = true;
        continue;
      }
      default:
        ++metrics.triangulated_faces;
      }
    }
    if (rebuild) {
      top = topology(source, selection, false, metrics);
      mapping_changed = true;
    } else if (edited)
      top = std::move(edited);
  }
  if (velocities) {
    const auto *attribute = source.findPointAttribute("v");
    numeric(attribute, "v", 3);
    if (!data.positions.empty() && !attribute)
      throw std::invalid_argument("Velocity blur requires a three-component float point v attribute: " +
                                  request.sop_path);
    GA_ROHandleV3 v(attribute);
    velocities->reserve(data.positions.size());
    for (const auto index : top->source_points)
      velocities->push_back(value(v.get(source.pointOffset(index))));
    // Acceleration mode: point accel in the same order; absent leaves it empty.
    const auto *accel = accelerations ? source.findPointAttribute("accel") : nullptr;
    if (accel && accel->getStorageClass() == GA_STORECLASS_FLOAT && accel->getTupleSize() == 3) {
      GA_ROHandleV3 a(accel);
      accelerations->reserve(data.positions.size());
      for (const auto index : top->source_points)
        accelerations->push_back(value(a.get(source.pointOffset(index))));
    }
  }
  if (identities) {
    NativeIdentityReader reader(source.findPointAttribute("name"), source.findPointAttribute("id"),
                                selection ? selection->point_range(source) : source.getPointRange());
    std::int64_t local_index = 0;
    for (const auto index : top->source_points) {
      std::uint64_t fallback = 0;
      identities->keys.push_back(reader.read(source.pointOffset(index), local_index++, fallback));
      identities->index_fallback |= fallback > 0;
    }
  }
  std::optional<bool> closed_control_mesh;
  if (subdivided) {
    data.subdivision.emplace();
    data.subdivision->settings = request.subdivision;
    if (request.subdivision.type == SubdivisionType::CatmullClark &&
        request.subdivision.boundary == SubdivisionBoundary::None) {
      if (reuse)
        closed_control_mesh = previous->closed_control_mesh;
      if (!closed_control_mesh) {
        std::map<std::pair<int, int>, int> edges;
        for (const auto &face : top->faces)
          for (int corner = 0; corner < face.corner_count; ++corner) {
            const int a = top->corner_points[face.first_corner + corner],
                      b = top->corner_points[face.first_corner + (corner + 1) % face.corner_count];
            const auto edge = std::minmax(a, b);
            ++edges[{edge.first, edge.second}];
          }
        closed_control_mesh =
            std::none_of(edges.begin(), edges.end(), [](const auto &edge) { return edge.second == 1; });
      }
      if (!*closed_control_mesh) {
        data.subdivision->settings.boundary = SubdivisionBoundary::EdgeAndCorner;
        if (diagnostics)
          diagnostics->push_back(
              {"geometry.subdivision_boundary", request.sop_path, request.geometry_key,
               "No-boundary Catmull-Clark requires a closed control mesh; this source has open edges",
               "Using Edges and Corners boundary interpolation; retaining the subdivision mesh"});
      }
    }
    if (diagnostics) {
      if (const auto *holes = source.findPrimitiveGroup("subdivision_hole"); holes && !holes->isEmpty())
        diagnostics->push_back(
            {"geometry.unsupported_subdivision_holes", request.sop_path, request.geometry_key,
             "The subdivision_hole group is not supported by this adapter",
             "Ignoring the hole designation; keeping the faces and using Cycles subdivision settings"});
      std::string overrides;
      for (const auto *name : {"osd_scheme", "osd_vtxboundaryinterpolation", "osd_fvarlinearinterpolation",
                               "osd_creasingmethod", "osd_trianglesubdiv"})
        if (source.findGlobalAttribute(name)) {
          if (!overrides.empty())
            overrides += ", ";
          overrides += name;
        }
      if (!overrides.empty())
        diagnostics->push_back(
            {"geometry.unsupported_subdivision_metadata", request.sop_path, request.geometry_key,
             "Unsupported subdivision detail overrides: " + overrides,
             "Ignoring these overrides; retaining geometry and the configured Cycles subdivision settings"});
    }
  }
  // Adaptive sources also provide an original-polygon preview when updates
  // are off. Retain its authored normals; tessellated Catmull-Clark uses its
  // own limit normals when the core fills the subdivision attributes.
  const bool limit_normals =
      subdivided && request.subdivision.type == SubdivisionType::CatmullClark && !request.subdivision.adaptive;
  const auto *vertex_normal = limit_normals ? nullptr : source.findVertexAttribute("N"),
             *point_normal = limit_normals ? nullptr : source.findPointAttribute("N");
  // Vertex attributes take precedence. An unused point attribute must not
  // invalidate the effective vertex stream, or silently replace a broken one.
  const auto *normal = vertex_normal ? vertex_normal : point_normal;
  std::optional<SourceDiagnostic> normal_diagnostic;
  const auto invalid_normal = [&](const std::string &reason) {
    normal_diagnostic = SourceDiagnostic{"geometry.invalid_normal", request.sop_path, request.geometry_key,
                                         std::string(vertex_normal ? "Vertex" : "Point") + " N " + reason,
                                         "Ignoring authored normals for this mesh; computing normals from geometry"};
  };
  if (normal && (normal->getStorageClass() != GA_STORECLASS_FLOAT || normal->getTupleSize() != 3))
    invalid_normal("must be a three-component float attribute");
  GA_ROHandleV3 vn(normal_diagnostic ? nullptr : vertex_normal),
      pn(normal_diagnostic || vertex_normal ? nullptr : point_normal);
  const auto *vertex_uv = shading_attributes ? source.findVertexAttribute("uv") : nullptr,
             *point_uv = shading_attributes ? source.findPointAttribute("uv") : nullptr;
  NativeShadingAttribute uv(vertex_uv ? vertex_uv : point_uv, true, request.sop_path, request.geometry_key);
  const auto *color_attribute = shading_attributes ? named_attribute(source, "Cd", true) : nullptr;
  NativeShadingAttribute color(color_attribute, false, request.sop_path, request.geometry_key);
  const auto normal_ids = attributes(vertex_normal, point_normal), uv_ids = attributes(vertex_uv, point_uv),
             color_ids = attributes(color_attribute, nullptr);
  const bool reuse_normals =
      reuse && !mapping_changed && previous->normal_ids == normal_ids && valid_attributes(vertex_normal, point_normal);
  const bool reuse_uv =
      reuse && !mapping_changed && previous->uv_ids == uv_ids && valid_attributes(vertex_uv, point_uv);
  const bool reuse_color =
      reuse && !mapping_changed && previous->color_ids == color_ids && valid_attributes(color_attribute, nullptr);
  if (reuse_normals) {
    data.corner_normals = previous->data->corner_normals;
    normal_diagnostic = previous->normal_diagnostic;
  }
  if (reuse_uv) {
    data.corner_uv = previous->data->corner_uv;
    uv.diagnostic = previous->uv_diagnostic;
  }
  if (reuse_color) {
    data.corner_colors = previous->data->corner_colors;
    color.diagnostic = previous->color_diagnostic;
  }
  // Shared immutable topology may come from another uniform material binding.
  // Retained source problems still describe this capture's resource identity.
  for (auto *issue : {&normal_diagnostic, &uv.diagnostic, &color.diagnostic})
    if (*issue) {
      (*issue)->source = request.sop_path;
      (*issue)->resource = request.geometry_key;
    }
  const std::size_t corners = subdivided ? top->corner_points.size() : top->triangle_corners.size() * 3;
  if (!reuse_normals && (vn.isValid() || pn.isValid()))
    data.corner_normals.reserve(corners);
  if (!reuse_uv && uv)
    data.corner_uv.reserve(corners);
  if (!reuse_color && color)
    data.corner_colors.reserve(corners);
  NativeNamedAttributes named(source, request, true, shading_attributes);
  // GA attribute IDs are only meaningful together with topology IDs: a
  // defragmentation can preserve attribute IDs while changing host offsets.
  // Exact logical topology fallback still retains triangulation, but it is
  // deliberately insufficient to reuse an old native named-field buffer.
  if (reuse && !mapping_changed && ids[0] != GA_INVALID_DATAID && ids[1] != GA_INVALID_DATAID &&
      previous->topology_ids == ids)
    named.reuse(previous->named.get(), previous->data->attributes, corners);
  named.reserve(corners);
  const auto corner = [&](int index) {
    if (named.needs_read()) {
      const auto named_vertex = source.vertexOffset(top->corner_vertices[index]);
      named.read(named_vertex, source.vertexPoint(named_vertex), source.vertexPrimitive(named_vertex));
    }
    if (reuse_normals && reuse_uv && reuse_color)
      return;
    const auto vertex = source.vertexOffset(top->corner_vertices[index]);
    const auto point = source.pointOffset(top->source_points[top->corner_points[index]]);
    if (!reuse_normals && !normal_diagnostic && (vn.isValid() || pn.isValid())) {
      const auto n = value(vn.isValid() ? vn.get(vertex) : pn.get(point));
      if (!std::isfinite(n.x) || !std::isfinite(n.y) || !std::isfinite(n.z))
        invalid_normal("contains a non-finite component");
      else if (n.x == 0 && n.y == 0 && n.z == 0)
        invalid_normal("contains a zero-length vector");
      else
        data.corner_normals.push_back(n);
    }
    if (!reuse_uv && uv) {
      const auto value = uv.read(vertex_uv ? vertex : point);
      data.corner_uv.push_back({value.x, value.y});
    }
    if (!reuse_color && color)
      data.corner_colors.push_back(color.read_element(vertex, point, source.vertexPrimitive(vertex)));
  };
  NativeMaterialBindings bindings(source, request, selection);
  if (!request.material_key.empty())
    data.materials = {request.material_key};
  const bool creased = subdivided && request.subdivision.use_creases;
  const auto *vertex_weights = creased ? source.findVertexAttribute("creaseweight") : nullptr;
  const auto *edge_weights =
      vertex_weights ? vertex_weights : (creased ? source.findPrimitiveAttribute("creaseweight") : nullptr);
  const auto *corner_weights = creased ? source.findPointAttribute("cornerweight") : nullptr;
  std::set<std::string> crease_issues;
  const auto crease_issue = [&](const char *name, const std::string &reason) {
    if (diagnostics && crease_issues.insert(name).second)
      diagnostics->push_back({std::string("geometry.invalid_") + name, request.sop_path, request.geometry_key,
                              std::string(name) + " " + reason,
                              "Ignoring unusable weights; retaining valid creases and the subdivision mesh"});
  };
  const auto crease_handle = [&](const GA_Attribute *attribute, const char *name) {
    if (attribute && (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != 1)) {
      crease_issue(name, "must be a scalar float attribute");
      return GA_ROHandleF{};
    }
    return GA_ROHandleF(attribute);
  };
  const auto edge_weight = crease_handle(edge_weights, "creaseweight"),
             corner_weight = crease_handle(corner_weights, "cornerweight");
  const auto sharpness = [&](float weight, const char *name) {
    if (!std::isfinite(weight) || weight < 0) {
      crease_issue(name, "contains non-finite or negative values");
      return 0.f;
    }
    return std::min(weight, 10.f);
  };
  std::map<std::pair<int, int>, float> creases;
  const bool reuse_triangles = reuse && !mapping_changed && !subdivided;
  if (reuse_triangles)
    data.triangles = previous->data->triangles;
  else
    data.triangles.reserve(top->triangle_corners.size());
  if (!subdivided && !named.needs_read()) {
    // Triangle streams on all cores. Triangle t of the mesh owns corner
    // slots 3t..3t+2 (c, b, a: reversed winding), the serial push order.
    // A stream's first invalid value in that order chooses its diagnostic,
    // exactly as the serial walk, which stops reading that stream there.
    const auto triangles = top->triangle_corners.size();
    const bool read_normals = !reuse_normals && !normal_diagnostic && (vn.isValid() || pn.isValid());
    const bool read_uv = !reuse_uv && bool(uv), read_color = !reuse_color && bool(color);
    if (!reuse_triangles)
      data.triangles.resize(triangles);
    if (read_normals)
      data.corner_normals.resize(corners);
    if (read_uv)
      data.corner_uv.resize(corners);
    if (read_color)
      data.corner_colors.resize(corners);
    // A detail Cd is one value: every corner would read GA_DETAIL_OFFSET.
    const bool constant_color = read_color && color.owner == GA_ATTRIB_DETAIL;
    Float3 detail_color;
    const int detail_color_code = constant_color ? color.peek(GA_DETAIL_OFFSET, detail_color) : 0;
    constexpr auto none = std::numeric_limits<std::uint64_t>::max();
    std::atomic<std::uint64_t> normal_failure{none}, uv_failure{none}, color_failure{none};
    // Raw pointers: tasks must not reload vector members through shared objects.
    const auto *faces = top->faces.data();
    const auto *corners_of = top->triangle_corners.data();
    const int *corner_points = top->corner_points.data(), *source_points = top->source_points.data();
    const auto *corner_vertices = top->corner_vertices.data();
    Triangle *triangle_out = reuse_triangles ? nullptr : data.triangles.data();
    Float3 *normal_out = read_normals ? data.corner_normals.data() : nullptr;
    Float2 *uv_out = read_uv ? data.corner_uv.data() : nullptr;
    Float3 *color_out = read_color ? data.corner_colors.data() : nullptr;
    const bool per_corner = read_normals || read_uv || (read_color && !constant_color);
    if (triangle_out || normal_out || uv_out || color_out)
      mesh_slots(top->faces.size(), 1024, [&](std::size_t begin, std::size_t end) {
        for (auto f = begin; f < end; ++f) {
          const auto &face = faces[f];
          for (int t = 0; t < face.triangle_count; ++t) {
            const auto index = std::size_t(face.first_triangle + t);
            const auto &triangle = corners_of[index];
            if (triangle_out)
              triangle_out[index] = {corner_points[triangle.c], corner_points[triangle.b], corner_points[triangle.a]};
            if (constant_color)
              for (std::size_t k = 0; k < 3; ++k) {
                if (detail_color_code)
                  first_failure(color_failure, 3 * index + k, detail_color_code);
                color_out[3 * index + k] = detail_color;
              }
            if (!per_corner)
              continue;
            const int order[3] = {triangle.c, triangle.b, triangle.a};
            for (int k = 0; k < 3; ++k) {
              const auto slot = 3 * index + std::size_t(k);
              const auto vertex = source.vertexOffset(corner_vertices[order[k]]);
              const auto point = source.pointOffset(source_points[corner_points[order[k]]]);
              if (normal_out) {
                const auto n = value(vn.isValid() ? vn.get(vertex) : pn.get(point));
                if (!std::isfinite(n.x) || !std::isfinite(n.y) || !std::isfinite(n.z))
                  first_failure(normal_failure, slot, 1);
                else if (n.x == 0 && n.y == 0 && n.z == 0)
                  first_failure(normal_failure, slot, 2);
                normal_out[slot] = n;
              }
              if (uv_out) {
                Float3 value;
                if (const int code = uv.peek(vertex_uv ? vertex : point, value))
                  first_failure(uv_failure, slot, code);
                uv_out[slot] = {value.x, value.y};
              }
              if (color_out && !constant_color) {
                Float3 value;
                if (const int code = color.peek(color.element(vertex, point, source.vertexPrimitive(vertex)), value))
                  first_failure(color_failure, slot, code);
                color_out[slot] = value;
              }
            }
          }
        }
      });
    if (const auto failure = normal_failure.load(); failure != none)
      invalid_normal((failure & 3) == 1 ? "contains a non-finite component" : "contains a zero-length vector");
    if (const auto failure = uv_failure.load(); failure != none)
      uv.fail_code(int(failure & 3));
    if (const auto failure = color_failure.load(); failure != none)
      color.fail_code(int(failure & 3));
    // A primitive's binding is resolved (and interned) by its first call;
    // later calls for the same primitive return that slot.
    if (request.material_mode == NativeMaterialMode::Native) {
      data.triangle_material.reserve(triangles);
      for (const auto &face : top->faces)
        if (face.triangle_count > 0)
          data.triangle_material.insert(data.triangle_material.end(), std::size_t(face.triangle_count),
                                        bindings.primitive(source.primitiveOffset(face.primitive)));
    }
  } else
    for (const auto &face : top->faces) {
      const auto primitive = source.primitiveOffset(face.primitive);
      if (subdivided) {
        auto &subd = *data.subdivision;
        subd.face_sizes.push_back(face.corner_count);
        subd.face_provenance.push_back({face.primitive, face.polygon});
        if (request.material_mode == NativeMaterialMode::Native)
          subd.face_material.push_back(bindings.primitive(primitive));
        for (int c = face.corner_count - 1; c >= 0; --c) {
          const auto index = face.first_corner + c;
          corner(index);
          const int point = top->corner_points[index];
          subd.face_corners.push_back(point);
          const int other = top->corner_points[face.first_corner + (c + 1) % face.corner_count];
          const auto vertex = source.vertexOffset(top->corner_vertices[index]);
          const float weight = edge_weight.isValid()
                                   ? sharpness(edge_weight.get(vertex_weights ? vertex : primitive), "creaseweight")
                                   : 0;
          if (weight > 0) {
            const auto edge = std::minmax(point, other);
            auto &existing = creases[{edge.first, edge.second}];
            existing = std::max(existing, weight);
          }
        }
      } else
        for (int t = 0; t < face.triangle_count; ++t) {
          const auto &triangle = top->triangle_corners[face.first_triangle + t];
          // Reverse winding together with all face-varying data.
          if (!reuse_triangles)
            data.triangles.push_back(
                {top->corner_points[triangle.c], top->corner_points[triangle.b], top->corner_points[triangle.a]});
          corner(triangle.c);
          corner(triangle.b);
          corner(triangle.a);
          if (request.material_mode == NativeMaterialMode::Native)
            data.triangle_material.push_back(bindings.primitive(primitive));
        }
    }
  if (subdivided) {
    for (const auto &[edge, weight] : creases)
      data.subdivision->edge_creases.push_back({edge.first, edge.second, weight});
    if (corner_weight.isValid())
      for (std::size_t i = 0; i < top->source_points.size(); ++i) {
        const auto weight = sharpness(corner_weight.get(source.pointOffset(top->source_points[i])), "cornerweight");
        if (weight > 0)
          data.subdivision->vertex_creases.push_back({int(i), weight});
      }
  }
  if (normal_diagnostic) {
    data.corner_normals.clear();
    if (diagnostics)
      diagnostics->push_back(*normal_diagnostic);
  }
  uv.finish(data.corner_uv, diagnostics);
  color.finish(data.corner_colors, diagnostics);
  auto named_cache = cache ? named.cache() : nullptr;
  metrics.named_fields_captured += named.fields_captured();
  metrics.named_fields_reused += named.fields_reused();
  metrics.named_values_read += named.values_read();
  metrics.named_values_reused += named.values_reused();
  named.finish(data.attributes, request, diagnostics);
  if (request.pointiness &&
      std::all_of(std::as_const(data.positions).begin(), std::as_const(data.positions).end(),
                  [](const auto p) { return std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z); })) {
    const bool same_positions = reuse && data.positions == previous->data->positions;
    if (same_positions && !previous->data->pointiness.empty())
      data.pointiness = previous->data->pointiness;
    else
      data.pointiness = mesh_pointiness(
          std::as_const(data.positions),
          [&](auto emit) {
            for (const auto &face : top->faces)
              emit(std::span<const int>(top->corner_points).subspan(face.first_corner, face.corner_count));
          },
          true);
  }
  if (request.material_mode == NativeMaterialMode::Native) {
    data.materials = bindings.materials();
    if (material_references)
      *material_references = bindings.references();
  }
  if (cache) {
    auto next = std::make_shared<NativeMeshCaptureCache>();
    next->topology = std::move(top);
    next->topology_ids = std::move(ids);
    next->position_id = position_id;
    next->full_detail = !selection || selection->full_detail;
    next->named = std::move(named_cache);
    next->normal_ids = normal_ids;
    next->uv_ids = uv_ids;
    next->color_ids = color_ids;
    next->subdivision = request.subdivision;
    next->closed_control_mesh = closed_control_mesh;
    next->subdivision_topology_error = std::move(subdivision_error);
    next->normal_diagnostic = std::move(normal_diagnostic);
    next->uv_diagnostic = uv.diagnostic;
    next->color_diagnostic = color.diagnostic;
    *cache = std::move(next);
  }
  return data;
}
} // namespace cycles_houdini
