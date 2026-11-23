/*
 * Copyright (c) 2026
 *	Side Effects Software Inc.  All rights reserved.
 *
 * Redistribution and use of Houdini Development Kit samples in source and
 * binary forms, with or without modification, are permitted provided that the
 * following conditions are met:
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. The name of Side Effects Software may not be used to endorse or
 *    promote products derived from this software without specific prior
 *    written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY SIDE EFFECTS SOFTWARE `AS IS' AND ANY EXPRESS
 * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
 * OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.  IN
 * NO EVENT SHALL SIDE EFFECTS SOFTWARE BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
 * OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
 * LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
 * EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 *----------------------------------------------------------------------------
 */

// Registration and HDK interface based on the installed HDK samples.
// SPDX-License-Identifier: Apache-2.0
#include "packed_blender_reference.h"
#include "packed_blender_bounds.h"
#include <GU/GU_PackedFactory.h>
#include <GU/GU_Detail.h>
#include <GU/GU_Types.h>
#include <GU/GU_PrimPacked.h>
#include <GU/GU_PrimPoly.h>
#include <GT/GT_GEOAttributeFilter.h>
#include <GT/GT_GEODetail.h>
#include <GT/GT_GEODetailList.h>
#include <GT/GT_GEOPrimCollect.h>
#include <GT/GT_GEOSupport.h>
#include <GT/GT_PrimCollect.h>
#include <GT/GT_PrimInstance.h>
#include <GT/GT_RefineParms.h>
#include <GT/GT_TransformArray.h>
#include <UT/UT_JSONValue.h>
#include <UT/UT_MemoryCounter.h>
#include <cmath>
#include <deque>
#include <map>
#include <limits>
#include <mutex>
#include <set>
#include <vector>

namespace cycles_houdini {
struct BlenderReferenceHull {
  std::string serialized;
  GU_ConstDetailHandle detail;
  UT_BoundingBox box;
  size_t memory_bytes = 0;
  int64 points = 0, triangles = 0;
};
namespace {
// Bounded, immutable viewport prototypes. A copied packed implementation shares
// its prototype; redraw/getPackedDetail never parses or evaluates geometry.
constexpr size_t max_hull_bytes = 2 * 1024 * 1024;
constexpr size_t max_hull_cache_bytes = 16 * 1024 * 1024;
std::mutex hull_mutex;
std::map<std::string, std::shared_ptr<const BlenderReferenceHull>> hull_cache;
std::deque<std::string> hull_order;
size_t hull_cache_bytes = 0;
std::shared_ptr<const BlenderReferenceHull> viewportHull(const std::string &text) {
  if (text.empty() || text.size() > max_hull_bytes || text.find('\0') != std::string::npos)
    return {};
  std::lock_guard<std::mutex> lock(hull_mutex);
  const auto cached = hull_cache.find(text);
  if (cached != hull_cache.end())
    return cached->second;
  UT_JSONValue value;
  if (!value.parseValue(UT_StringHolder(text)) || !value.isMap())
    return {};
  const auto pieces = value.find("pieces");
  if (pieces.atEnd() || !pieces->isArray())
    return {};
  std::unique_ptr<GU_Detail> detail(new GU_Detail);
  size_t piece_count = 0, vertex_count = 0, triangle_count = 0;
  for (const auto &piece : *pieces) {
    if (!piece.isMap() || ++piece_count > 128)
      return {};
    const auto points = piece.find("points"), triangles = piece.find("triangles");
    if (points.atEnd() || !points->isArray() || triangles.atEnd() || !triangles->isArray())
      return {};
    std::vector<GA_Offset> offsets;
    double xyz[3];
    size_t coordinate_count = 0;
    for (const auto &coordinate : *points) {
      if ((coordinate.getType() != UT_JSONValue::JSON_INT && coordinate.getType() != UT_JSONValue::JSON_REAL) ||
          !std::isfinite(coordinate.getF()) || std::abs(coordinate.getF()) > std::numeric_limits<float>::max())
        return {};
      xyz[coordinate_count++ % 3] = coordinate.getF();
      if (coordinate_count % 3 == 0) {
        if (++vertex_count > 16384)
          return {};
        const auto offset = detail->appendPointOffset();
        detail->setPos3(offset, UT_Vector3D(xyz[0], xyz[1], xyz[2]));
        offsets.push_back(offset);
      }
    }
    if (coordinate_count % 3 || offsets.empty())
      return {};
    int indices[3];
    size_t index_count = 0;
    for (const auto &index : *triangles) {
      if (index.getType() != UT_JSONValue::JSON_INT || index.getI() < 0 || size_t(index.getI()) >= offsets.size())
        return {};
      indices[index_count++ % 3] = int(index.getI());
      if (index_count % 3 == 0) {
        if (++triangle_count > 32768)
          return {};
        auto *polygon = GU_PrimPoly::build(detail.get(), 3, GU_POLY_CLOSED, false);
        for (int i = 0; i < 3; ++i)
          polygon->setPointOffset(i, offsets[indices[i]]);
      }
    }
    if (index_count % 3)
      return {};
  }
  if (!triangle_count)
    return {};
  // These nested details are authored outside a SOP cook, so no SOP bumps
  // their GA data IDs for us. Finalize before publishing the immutable handle;
  // GT/viewport caches require valid topology and attribute revisions.
  detail->markDirty(true);
  auto result = std::make_shared<BlenderReferenceHull>();
  result->serialized = text;
  result->points = int64(vertex_count);
  result->triangles = int64(triangle_count);
  detail->getBBox(&result->box);
  GU_DetailHandle handle;
  handle.allocateAndSet(detail.release());
  result->detail = handle;
  // Account for serialized keys plus a conservative mesh budget.
  const size_t bytes = text.size() * 2 + vertex_count * 128 + triangle_count * 256;
  result->memory_bytes = bytes;
  while (!hull_order.empty() && (hull_cache.size() >= 64 || hull_cache_bytes + bytes > max_hull_cache_bytes)) {
    const auto oldest = hull_cache.find(hull_order.front());
    if (oldest != hull_cache.end()) {
      hull_cache_bytes -= oldest->second->memory_bytes;
      hull_cache.erase(oldest);
    }
    hull_order.pop_front();
  }
  hull_cache.emplace(text, result);
  hull_order.push_back(text);
  hull_cache_bytes += result->memory_bytes;
  return result;
}
GU_ConstDetailHandle fallbackViewportBox() {
  // Built once while attaching preview data; never generated during redraw.
  static const auto box = viewportHull(
      R"({"pieces":[{"points":[-0.5,-0.5,-0.5,0.5,-0.5,-0.5,0.5,0.5,-0.5,-0.5,0.5,-0.5,-0.5,-0.5,0.5,0.5,-0.5,0.5,0.5,0.5,0.5,-0.5,0.5,0.5],"triangles":[0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7]}]})");
  return box->detail;
}
GA_PrimitiveTypeId primitive_type(-1);

// Shared GU handles alone do not enable viewport instancing: the generic
// custom-packed collector emits one non-instancable GT wrapper per copy.
// Keep source attributes on the instance list, never on its shared hull.
struct ViewportHullCollectData final : GT_GEOPrimCollectData {
  struct Group {
    GU_ConstDetailHandle detail;
    GT_GEOOffsetList offsets;
  };
  bool viewport = false;
  int override_lod = -1;
  GT_GEOPrimCollectData *original_data = nullptr;
  std::mutex groups_mutex;
  std::map<exint, Group> groups;
  ~ViewportHullCollectData() override { delete original_data; }
};

class ViewportHullCollector final : public GT_GEOPrimCollect {
public:
  explicit ViewportHullCollector(GT_GEOPrimCollectPtr original) : original_(std::move(original)) {}

  GT_GEOPrimCollectData *beginCollecting(const GT_GEODetailListHandle &geometry,
                                         const GT_RefineParms *parms) const override {
    auto *data = new ViewportHullCollectData;
    // Rendering/export refinement retains the existing packed contract. The
    // cosmetic proxy is only collected for a single viewport sample.
    data->viewport = GT_RefineParms::getPackedViewportLOD(parms) && geometry->entries() == 1;
    data->override_lod = int(GT_RefineParms::getOverridePackedViewportLOD(parms));
    data->original_data = original_->beginCollecting(geometry, parms);
    return data;
  }

  GT_PrimitiveHandle collect(const GT_GEODetailListHandle &geometry, const GEO_Primitive *const *primitives,
                             int segments, GT_GEOPrimCollectData *raw_data) const override {
    auto *data = static_cast<ViewportHullCollectData *>(raw_data);
    const auto *packed = static_cast<const GU_PrimPacked *>(primitives[0]);
    const auto lod = data->override_lod >= 0 ? GEO_ViewportLOD(data->override_lod) : packed->viewportLOD();
    if (!data->viewport || lod != GEO_VIEWPORT_FULL)
      return original_->collect(geometry, primitives, segments, data->original_data);
    const auto detail = packed->getPackedDetail();
    if (!detail.isValid())
      return original_->collect(geometry, primitives, segments, data->original_data);
    // Each source attachment owns an immutable detail; copied primitives share
    // it. Wire/shaded variants have distinct detail identities automatically.
    std::lock_guard<std::mutex> lock(data->groups_mutex);
    auto &group = data->groups[detail.gdp()->getUniqueId()];
    group.detail = detail;
    group.offsets.append(packed->getMapOffset());
    return {};
  }

  GT_PrimitiveHandle endCollecting(const GT_GEODetailListHandle &geometry,
                                   GT_GEOPrimCollectData *raw_data) const override {
    auto *data = static_cast<ViewportHullCollectData *>(raw_data);
    const auto fallback = original_->endCollecting(geometry, data->original_data);
    if (data->groups.empty())
      return fallback;
    GT_PrimitiveHandle combined = new GT_PrimCollect;
    auto *collection = static_cast<GT_PrimCollect *>(combined.get());
    const auto source = geometry->getGeometry(0);
    const auto *gdp = source.gdp();
    for (const auto &[identity, group] : data->groups) {
      UT_Matrix4DArray matrices;
      GT_GEOOffsetList vertices;
      for (exint index = 0; index < group.offsets.entries(); ++index) {
        const auto *packed = static_cast<const GU_PrimPacked *>(gdp->getPrimitive(group.offsets(index)));
        matrices.append();
        packed->getFullTransform4(matrices.last());
        vertices.append(packed->getVertexOffset(0));
      }
      GT_TransformArrayHandle transforms = new GT_TransformArray;
      transforms->setTransforms(matrices);
      GT_GEOAttributeFilter filter;
      const auto uniform = geometry->getPrimitiveVertexAttributes(
          filter, group.offsets, vertices, GT_GEODetailList::GEO_INCLUDE_POINT, GT_GEODetailList::GEO_SKIP_DETAIL);
      const auto detail_attributes = geometry->getDetailAttributes(filter, true);
      // The prototype owns the immutable hull, independently of source SOP
      // primitive storage which may be reused on a later cook. It has no packed
      // transform: apply each complete source/instance transform exactly once.
      GT_GEODetailListHandle hull_source(new GT_GEODetailList(group.detail));
      GT_PrimitiveHandle prototype = GT_GEODetail::makeDetail(hull_source, nullptr);
      collection->appendPrimitive(
          new GT_PrimInstance(prototype, transforms, group.offsets, uniform, detail_attributes, hull_source));
    }
    if (fallback)
      collection->appendPrimitive(fallback);
    return collection->entries() == 1 ? collection->getPrim(0) : combined;
  }

private:
  GT_GEOPrimCollectPtr original_;
};

class ReferenceFactory final : public GU_PackedFactory {
public:
  ReferenceFactory()
      : GU_PackedFactory(PackedBlenderReference::typeName, "Cycles Blender Reference"),
        default_(new PackedBlenderReference) {
    registerIntrinsic("cycles_blend_file", StringGetterCast(&PackedBlenderReference::intrinsicFile),
                      StringSetterCast(&PackedBlenderReference::setFile));
    registerIntrinsic("cycles_blend_target", StringGetterCast(&PackedBlenderReference::intrinsicTarget),
                      StringSetterCast(&PackedBlenderReference::setTarget));
    registerIntrinsic("cycles_blend_target_kind", StringGetterCast(&PackedBlenderReference::intrinsicKind),
                      StringSetterCast(&PackedBlenderReference::setKind));
    registerIntrinsic("cycles_blender_reference", StdStringGetterCast(&PackedBlenderReference::intrinsicReference));
    registerIntrinsic("cycles_blender_frame", FloatGetterCast(&PackedBlenderReference::intrinsicFrame),
                      FloatSetterCast(&PackedBlenderReference::setFrame));
    registerIntrinsic("cycles_blender_shape_frame", FloatGetterCast(&PackedBlenderReference::intrinsicShapeFrame),
                      FloatSetterCast(&PackedBlenderReference::setShapeFrame));
    registerIntrinsic("cycles_blender_shape_bounds",
                      StdStringGetterCast(&PackedBlenderReference::intrinsicShapeBounds));
    registerIntrinsic("cycles_blender_viewport_wireframe",
                      BoolGetterCast(&PackedBlenderReference::intrinsicViewportWireframe),
                      BoolSetterCast(&PackedBlenderReference::setViewportWireframe));
    registerIntrinsic("cycles_blender_viewport_detail_id",
                      IntGetterCast(&PackedBlenderReference::intrinsicViewportDetailId));
    registerIntrinsic("cycles_blender_viewport_shape",
                      StringGetterCast(&PackedBlenderReference::intrinsicViewportShape),
                      StringSetterCast(&PackedBlenderReference::setViewportShape));
    registerIntrinsic("cycles_blender_viewport_hull",
                      StdStringGetterCast(&PackedBlenderReference::intrinsicViewportHull));
    registerIntrinsic("cycles_blender_hull_per_member",
                      BoolGetterCast(&PackedBlenderReference::intrinsicHullPerMember));
    registerIntrinsic("cycles_blender_viewport_hull_points",
                      IntGetterCast(&PackedBlenderReference::intrinsicHullPoints));
    registerIntrinsic("cycles_blender_viewport_hull_triangles",
                      IntGetterCast(&PackedBlenderReference::intrinsicHullTriangles));
    registerIntrinsic("cycles_blender_bounds_known", BoolGetterCast(&PackedBlenderReference::intrinsicBoundsKnown));
    registerIntrinsic("cycles_blender_bounds_status", StringGetterCast(&PackedBlenderReference::intrinsicBoundsStatus));
    registerIntrinsic("cycles_blender_bounds", StdStringGetterCast(&PackedBlenderReference::intrinsicBounds),
                      StdStringSetterCast(&PackedBlenderReference::setBounds));
  }
  const UT_IntrusivePtr<GU_PackedImpl> &defaultImpl() const override { return default_; }
  GU_PackedImpl *create() const override { return new PackedBlenderReference; }

private:
  UT_IntrusivePtr<GU_PackedImpl> default_;
};
ReferenceFactory *factory = nullptr;
} // namespace

void PackedBlenderReference::install(GA_PrimitiveFactory *table) {
  if (factory)
    return;
  factory = new ReferenceFactory;
  GU_PrimPacked::registerPacked(table, factory);
  if (factory->isRegistered()) {
    primitive_type = factory->typeDef().getId();
    const auto original = GT_GEOPrimCollect::getCollector(primitive_type);
    if (original)
      (new ViewportHullCollector(original))->bind(primitive_type);
  }
}
const GA_PrimitiveTypeId &PackedBlenderReference::typeId() { return primitive_type; }
GU_PackedFactory *PackedBlenderReference::getFactory() const { return factory; }
GU_PackedImpl *PackedBlenderReference::copy() const { return new PackedBlenderReference(*this); }

bool PackedBlenderReference::isValid() const {
  return !file_.empty() && !target_.empty() &&
         (kind_ == "object" || kind_ == "collection" || kind_ == "collection+disabled") && std::isfinite(frame_);
}
std::string PackedBlenderReference::reference() const { return file_ + "#" + kind_ + "=" + target_; }
void PackedBlenderReference::dirty(GU_PrimPacked *prim, bool invalidate) {
  if (invalidate) {
    bounds_.reset();
    preview_.reset();
    shape_bounds_.reset();
    hull_.reset();
    viewport_detail_.clear();
    preview_lease_.reset();
  }
  clearBoxCache();
  if (prim)
    prim->topologyDirty();
}
GU_ConstDetailHandle PackedBlenderReference::getPackedDetail(GU_PackedContext *) const {
  if (viewport_shape_ != "hull" || !hull_)
    return {};
  return viewportPreviewValid() ? viewport_detail_ : fallback_detail_;
}
int64 PackedBlenderReference::intrinsicViewportDetailId(const GU_PrimPacked *) const {
  const auto detail = getPackedDetail();
  return detail.isValid() ? detail.gdp()->getUniqueId() : -1;
}
std::string PackedBlenderReference::intrinsicShapeBounds(const GU_PrimPacked *) const {
  return shape_bounds_ && viewportPreviewValid() ? shape_bounds_->serialized : std::string{};
}
std::string PackedBlenderReference::intrinsicViewportHull(const GU_PrimPacked *) const {
  return hull_ && viewportPreviewValid() ? hull_->serialized : std::string{};
}
int64 PackedBlenderReference::intrinsicHullPoints(const GU_PrimPacked *) const {
  return hull_ && viewportPreviewValid() ? hull_->points : 0;
}
int64 PackedBlenderReference::intrinsicHullTriangles(const GU_PrimPacked *) const {
  return hull_ && viewportPreviewValid() ? hull_->triangles : 0;
}
void PackedBlenderReference::setViewportShape(GU_PrimPacked *prim, const char *value) {
  const std::string next = value && std::string(value) == "hull" ? "hull" : "bounds";
  if (viewport_shape_ != next) {
    viewport_shape_ = next;
    dirty(prim, false);
  }
}
void PackedBlenderReference::setViewportHull(GU_PrimPacked *prim, const std::string &value) {
  if (hull_ && hull_->serialized == value)
    return;
  hull_ = viewportHull(value);
  rebuildViewportDetail();
  dirty(prim, false);
}
void PackedBlenderReference::rebuildViewportDetail() {
  viewport_detail_.clear();
  if (hull_) {
    // A source SOP recook replaces its packed primitive. Reusing the identical
    // nested GU detail across those owners can leave the viewport's old cached
    // packed geometry retired. Give this source attachment its own immutable
    // detail identity/revisions; packed copies still share this active handle.
    if (!viewport_wireframe_)
      viewport_detail_ = hull_->detail.getWriteableCopy(GA_DATA_ID_BUMP);
    else {
      auto detail = std::make_unique<GU_Detail>();
      const auto *source = hull_->detail.gdp();
      std::map<GA_Offset, GA_Offset> offsets;
      for (GA_Iterator it(source->getPointRange()); !it.atEnd(); ++it) {
        const auto offset = detail->appendPointOffset();
        detail->setPos3(offset, source->getPos3(*it));
        offsets.emplace(*it, offset);
      }
      std::set<std::pair<GA_Offset, GA_Offset>> edges;
      for (GA_Iterator it(source->getPrimitiveRange()); !it.atEnd(); ++it) {
        const auto *polygon = source->getPrimitive(*it);
        const auto count = polygon->getVertexCount();
        for (GA_Size i = 0; i < count; ++i) {
          const auto a = polygon->getPointOffset(i), b = polygon->getPointOffset((i + 1) % count);
          if (a != b)
            edges.emplace(std::min(a, b), std::max(a, b));
        }
      }
      for (const auto &[a, b] : edges) {
        auto *line = GU_PrimPoly::build(detail.get(), 2, GU_POLY_OPEN, false);
        line->setPointOffset(0, offsets.at(a));
        line->setPointOffset(1, offsets.at(b));
      }
      detail->markDirty(true);
      GU_DetailHandle handle;
      handle.allocateAndSet(detail.release());
      viewport_detail_ = handle;
    }
    fallback_detail_ = fallbackViewportBox();
  }
}
void PackedBlenderReference::setViewportWireframe(GU_PrimPacked *prim, bool value) {
  if (viewport_wireframe_ == value)
    return;
  viewport_wireframe_ = value;
  rebuildViewportDetail();
  dirty(prim, false);
}
void PackedBlenderReference::setFile(GU_PrimPacked *prim, const char *value) {
  const std::string next(value ? value : "");
  if (file_ != next) {
    file_ = next;
    dirty(prim);
  }
}
void PackedBlenderReference::setTarget(GU_PrimPacked *prim, const char *value) {
  const std::string next(value ? value : "");
  if (target_ != next) {
    target_ = next;
    dirty(prim);
  }
}
void PackedBlenderReference::setKind(GU_PrimPacked *prim, const char *value) {
  const std::string next(value ? value : "");
  if (kind_ != next) {
    kind_ = next;
    dirty(prim);
  }
}
void PackedBlenderReference::setFrame(GU_PrimPacked *prim, fpreal value) {
  if (!std::isfinite(value))
    return;
  if (frame_ != value) {
    frame_ = value;
    bounds_.reset();
    preview_.reset();
    dirty(prim, false);
  }
}
void PackedBlenderReference::setShapeFrame(GU_PrimPacked *prim, fpreal value) {
  if (!std::isfinite(value) || shape_frame_ == value)
    return;
  shape_frame_ = value;
  shape_bounds_.reset();
  hull_.reset();
  viewport_detail_.clear();
  preview_lease_.reset();
  dirty(prim, false);
}
void PackedBlenderReference::attachPreviewLease(const std::string &serialized) {
  preview_lease_ =
      serialized.empty() ? nullptr : lease_blender_reference_preview(reference(), shape_frame_, serialized);
}
bool PackedBlenderReference::viewportPreviewValid() const {
  return preview_lease_ && preview_lease_->valid.load(std::memory_order_relaxed);
}
bool PackedBlenderReference::knownBounds(UT_BoundingBox &box) const {
  if (!shape_bounds_ || !viewportPreviewValid()) {
    if (viewport_shape_ == "hull" && hull_ && viewportPreviewValid()) {
      box = hull_->box;
      return true;
    }
    return false;
  }
  const auto &values = shape_bounds_->box;
  box.initBounds(values[0], values[1], values[2]);
  box.enlargeBounds(values[3], values[4], values[5]);
  return true;
}
void PackedBlenderReference::setShapeBounds(GU_PrimPacked *prim, const std::string &value) {
  // Preview payloads may carry separately bounded hull metadata. Only their
  // compact dependency/bounds record participates in the validity lease.
  UT_JSONValue record;
  BlenderReferenceBounds bounds;
  std::string base;
  if (value.size() <= 2 * 1024 * 1024 + 128 * 1024 && record.parseValue(UT_StringHolder(value)) && record.isMap()) {
    record.erase("viewport_hulls");
    record.erase("viewport_hull_reason");
    record.erase("max_hull_vertices");
    base = record.toString().toStdString();
  }
  shape_bounds_ = isValid() && preview_blender_reference_bounds(reference(), shape_frame_, base, bounds)
                      ? std::make_shared<BlenderReferenceBounds>(std::move(bounds))
                      : nullptr;
  attachPreviewLease(shape_bounds_ ? base : std::string{});
  dirty(prim, false);
}
void PackedBlenderReference::setPreviewBounds(GU_PrimPacked *prim, const std::string &value) {
  BlenderReferenceBounds record;
  preview_ = isValid() && preview_blender_reference_bounds(reference(), frame_, value, record)
                 ? std::make_shared<BlenderReferenceBounds>(std::move(record))
                 : nullptr;
  if (!shape_bounds_ && shape_frame_ == frame_)
    setShapeBounds(prim, preview_ ? value : std::string{});
  dirty(prim, false);
}
void PackedBlenderReference::setBounds(GU_PrimPacked *prim, const std::string &value) {
  // Resident publications were validated before entering the cache. Attaching
  // their lease during a SOP cook must not repeat filesystem signature checks.
  const auto resident = lease_blender_reference_bounds(reference(), frame_);
  bounds_ = resident && resident->valid.load(std::memory_order_relaxed) && resident->bounds.serialized == value
                ? resident
                : nullptr;
  if (!bounds_)
    setPreviewBounds(prim, value);
  else if (!shape_bounds_ && shape_frame_ == frame_)
    setShapeBounds(prim, value);
  dirty(prim, false);
}
std::shared_ptr<const BlenderReferenceBoundsLease> PackedBlenderReference::retainedBounds() const {
  auto current = lease_blender_reference_bounds(reference(), frame_);
  if (current && current->valid.load(std::memory_order_relaxed))
    return current;
  return bounds_ && bounds_->valid.load(std::memory_order_relaxed) ? bounds_ : nullptr;
}
std::string PackedBlenderReference::intrinsicBounds(const GU_PrimPacked *) const {
  const auto lease = retainedBounds();
  return lease ? lease->bounds.serialized : preview_ ? preview_->serialized : std::string();
}
const char *PackedBlenderReference::intrinsicBoundsStatus(const GU_PrimPacked *) const {
  return retainedBounds() ? "validated" : preview_ ? "unverified" : "unknown";
}
bool PackedBlenderReference::intrinsicBoundsKnown(const GU_PrimPacked *) const { return bool(retainedBounds()); }
bool PackedBlenderReference::getRenderingBounds(UT_BoundingBox &box) const {
  // A saved viewport preview never becomes an authoritative render hull.
  const auto lease = retainedBounds();
  if (!lease)
    return false;
  const auto &values = lease->bounds.box;
  box.initBounds(values[0], values[1], values[2]);
  box.enlargeBounds(values[3], values[4], values[5]);
  return true;
}
bool PackedBlenderReference::getBounds(UT_BoundingBox &box) const {
  if (!knownBounds(box)) {
    box.initBounds(-0.5, -0.5, -0.5);
    box.enlargeBounds(0.5, 0.5, 0.5);
  }
  return true;
}
void PackedBlenderReference::update(GU_PrimPacked *prim, const UT_Options &options) {
  UT_StringHolder value;
  fpreal frame;
  if (import(options, "cycles_blend_file", value))
    setFile(prim, value.c_str());
  if (import(options, "cycles_blend_target", value))
    setTarget(prim, value.c_str());
  if (import(options, "cycles_blend_target_kind", value))
    setKind(prim, value.c_str());
  if (import(options, "cycles_blender_frame", frame))
    setFrame(prim, frame);
  if (import(options, "cycles_blender_shape_frame", frame))
    setShapeFrame(prim, frame);
  else
    setShapeFrame(prim, frame_); // Older packed files derived their shape from Source Frame.
  fpreal wireframe;
  if (import(options, "cycles_blender_viewport_wireframe", wireframe))
    setViewportWireframe(prim, wireframe != 0);
  fpreal per_member;
  if (import(options, "cycles_blender_hull_per_member", per_member))
    hull_per_member_ = per_member != 0;
  if (import(options, "cycles_blender_viewport_shape", value))
    setViewportShape(prim, value.c_str());
  if (import(options, "cycles_blender_viewport_hull", value))
    setViewportHull(prim, value.toStdString());
  if (import(options, "cycles_blender_bounds_preview", value))
    setPreviewBounds(prim, value.toStdString());
  if (import(options, "cycles_blender_bounds", value))
    setBounds(prim, value.toStdString());
  if (import(options, "cycles_blender_shape_bounds", value))
    setShapeBounds(prim, value.toStdString());
}
bool PackedBlenderReference::load(GU_PrimPacked *prim, const UT_Options &options, const GA_LoadMap &) {
  // Load only parses persisted metadata. Dependency validation is scheduled by
  // the owning SOP; loading geometry must not stat or evaluate its dependencies.
  UT_Options preview_options(options);
  UT_StringHolder saved;
  if (import(options, "cycles_blender_bounds", saved)) {
    preview_options.removeOption("cycles_blender_bounds");
    preview_options.setOptionS("cycles_blender_bounds_preview", saved);
  }
  update(prim, preview_options);
  // An incomplete/missing asset remains an editable reference after reopen.
  return true;
}
bool PackedBlenderReference::save(UT_Options &options, const GA_SaveMap &) const {
  // Serialize only existing metadata: HIP save never starts evaluation or validation.
  options.setOptionS("cycles_blend_file", file_);
  options.setOptionS("cycles_blend_target", target_);
  options.setOptionS("cycles_blend_target_kind", kind_);
  options.setOptionF("cycles_blender_frame", frame_);
  options.setOptionF("cycles_blender_shape_frame", shape_frame_);
  options.setOptionS("cycles_blender_shape_bounds", intrinsicShapeBounds(nullptr));
  options.setOptionF("cycles_blender_viewport_wireframe", viewport_wireframe_ ? 1.0 : 0.0);
  options.setOptionS("cycles_blender_bounds", intrinsicBounds(nullptr));
  options.setOptionS("cycles_blender_viewport_shape", viewport_shape_);
  options.setOptionF("cycles_blender_hull_per_member", hull_per_member_ ? 1.0 : 0.0);
  options.setOptionS("cycles_blender_viewport_hull", intrinsicViewportHull(nullptr));
  return true;
}
int64 PackedBlenderReference::getMemoryUsage(bool inclusive) const {
  return (inclusive ? sizeof(*this) : 0) + file_.capacity() + target_.capacity() + kind_.capacity() +
         viewport_shape_.capacity();
}
void PackedBlenderReference::countMemory(UT_MemoryCounter &counter, bool inclusive) const {
  if (counter.mustCountUnshared())
    counter.countUnshared(getMemoryUsage(inclusive));
}
const PackedBlenderReference *packed_blender_reference(const GU_PrimPacked &prim) noexcept {
  return dynamic_cast<const PackedBlenderReference *>(prim.sharedImplementation());
}
} // namespace cycles_houdini

void newGeometryPrim(GA_PrimitiveFactory *factory) { cycles_houdini::PackedBlenderReference::install(factory); }
