// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "geometry_selection.h"
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GU/GU_Detail.h>
#include <cmath>

namespace cycles_houdini {
inline const GA_Attribute *native_size_attribute(const GU_Detail &source, bool curves) {
  if (!curves)
    return source.findPointAttribute("pscale");
  for (const auto owner : {GA_ATTRIB_VERTEX, GA_ATTRIB_POINT, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
    if (const auto *attribute = source.findAttribute(owner, "width"))
      return attribute;
  return nullptr;
}
// pscale is a radius; width is a diameter. Repair only invalid entries, before
// conversion to engine radii. An invalid winning owner never exposes a lower
// priority attribute that the artist did not select.
struct NativeSizeAttribute {
  GA_ROHandleF values;
  const GA_Attribute *attribute{};
  bool invalid{};
  explicit NativeSizeAttribute(const GA_Attribute *source) : attribute(source) {
    if (!source)
      return;
    invalid = source->getStorageClass() != GA_STORECLASS_FLOAT || source->getTupleSize() != 1;
    if (!invalid) {
      values.bind(source);
      invalid = !values.isValid();
    }
  }
  static bool valid(float value) { return std::isfinite(value) && value >= 0; }
  float read(GA_Offset offset, float fallback) {
    if (!values.isValid())
      return fallback;
    return checked(values.get(offset), fallback);
  }
  float checked(float value, float fallback) {
    if (valid(value))
      return value;
    invalid = true;
    return fallback;
  }
  void finish(bool curves, const std::string &source, const std::string &resource,
              std::vector<SourceDiagnostic> *diagnostics) const {
    if (invalid && diagnostics)
      diagnostics->push_back(
          {curves ? "geometry.invalid_width" : "geometry.invalid_pscale", source, resource,
           std::string(curves ? "width" : "pscale") + " must contain finite, nonnegative scalar float values",
           std::string("Using the configured ") + (curves ? "curve width" : "point radius") +
               " for invalid entries; preserving valid sizes and geometry"});
  }
};
// A resource key includes the fallback only when it is actually consumed.
// Cache this classification by data IDs so shared prototypes and camera edits
// do not rescan all size values. IPR prewarms it on frozen background inputs.
class NativeSizeCache {
public:
  bool uses_fallback(const GU_Detail &source, const NativeGeometrySelection &selection) {
    const auto *attribute = native_size_attribute(source, selection.kind == NativeGeometryKind::Curves);
    const std::vector<std::int64_t> ids = {source.getTopology().getDataId(),
                                           source.getPrimitiveList().getDataId(),
                                           source.getNumPoints(),
                                           source.getNumPrimitives(),
                                           attribute ? attribute->getDataId() : 0,
                                           attribute ? attribute->getOwner() : -1,
                                           attribute ? attribute->getStorageClass() : -1,
                                           attribute ? attribute->getTupleSize() : 0};
    const bool reusable =
        ids[0] != GA_INVALID_DATAID && ids[1] != GA_INVALID_DATAID && (!attribute || ids[4] != GA_INVALID_DATAID);
    auto &record = records_[{source.getUniqueId(), selection.kind}];
    if (reusable && record.ids == ids)
      return record.fallback;
    NativeSizeAttribute sizes(attribute);
    bool fallback = !attribute || sizes.invalid;
    if (!fallback) {
      if (attribute->getOwner() == GA_ATTRIB_DETAIL)
        sizes.read(GA_DETAIL_OFFSET, 0);
      else {
        const auto range = attribute->getOwner() == GA_ATTRIB_VERTEX      ? selection.vertex_range(source)
                           : attribute->getOwner() == GA_ATTRIB_PRIMITIVE ? selection.primitive_range(source)
                                                                          : selection.point_range(source);
        for (GA_Iterator it(range); !it.atEnd() && !sizes.invalid; ++it)
          sizes.read(*it, 0);
      }
      fallback = sizes.invalid;
    }
    record = {ids, fallback};
    return fallback;
  }
  void clear() { records_.clear(); }
  void retire(const std::set<std::int64_t> &details) {
    std::erase_if(records_, [&](const auto &item) { return !details.contains(item.first.first); });
  }

private:
  struct Record {
    std::vector<std::int64_t> ids;
    bool fallback{};
  };
  std::map<std::pair<std::int64_t, NativeGeometryKind>, Record> records_;
};
} // namespace cycles_houdini
