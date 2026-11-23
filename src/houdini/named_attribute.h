// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene_adapter.h"
#include <algorithm>
#include <cmath>
#include <utility>
#include <GA/GA_AIFTuple.h>
#include <GU/GU_Detail.h>

namespace cycles_houdini {
// Only demanded fields are inspected. Houdini's most local authored domain
// wins; a broken vertex field must not silently select a different point field.
inline const GA_Attribute *named_attribute(const GU_Detail &source, const std::string &name, bool corners) {
  if (corners)
    if (const auto *a = source.findVertexAttribute(name.c_str()))
      return a;
  if (const auto *a = source.findPointAttribute(name.c_str()))
    return a;
  if (const auto *a = source.findPrimitiveAttribute(name.c_str()))
    return a;
  return source.findGlobalAttribute(name.c_str());
}
inline void named_attribute_identifiers(std::vector<std::int64_t> &ids, const GU_Detail &source,
                                        const GeometryRequest &request) {
  ids.push_back(request.pointiness);
  // Cd is captured and fingerprinted by the dedicated color stream even
  // before a shader requests it. A new Cd consumer must not recapture it.
  ids.push_back(std::count_if(request.shader_attributes.begin(), request.shader_attributes.end(),
                              [](const auto &name) { return name != "Cd"; }));
  for (const auto &name : request.shader_attributes) {
    if (name == "Cd")
      continue;
    ids.push_back(name.size());
    for (const unsigned char c : name)
      ids.push_back(c);
    for (const auto owner : {GA_ATTRIB_VERTEX, GA_ATTRIB_POINT, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL}) {
      const auto *a = source.findAttribute(owner, name.c_str());
      ids.push_back(a != nullptr);
      if (a)
        ids.insert(ids.end(), {a->getDataId(), a->getStorageClass(), a->getTupleSize()});
    }
  }
}
// The owning mesh cache validates topology IDs and emitted corner mapping.
// These records contain no host pointers; valid GA data IDs identify unchanged
// values, including invalid-source reasons which must survive cache hits.
struct NativeNamedAttributeStamp {
  std::int64_t data_id{-1};
  int owner{-1}, storage_class{-1}, storage{-1}, components{};
  bool tuple{};
  bool operator==(const NativeNamedAttributeStamp &) const = default;
};
struct NativeNamedAttributeCache {
  struct Field {
    std::string name;
    NativeNamedAttributeStamp stamp;
    std::string error;
  };
  std::vector<Field> fields;
  std::uint64_t bytes() const {
    std::uint64_t result = sizeof(*this) + fields.capacity() * sizeof(Field);
    for (const auto &field : fields)
      result += field.name.capacity() + field.error.capacity() + 2;
    return result;
  }
};
class NativeNamedAttributes {
  struct Field {
    const GA_Attribute *attribute{};
    NamedAttribute data;
    std::string error;
    NativeNamedAttributeStamp stamp;
    bool retained{};
  };
  std::vector<Field> fields_;
  std::uint64_t values_read_{};
  std::size_t readable_{};

public:
  explicit operator bool() const { return !fields_.empty(); }
  bool needs_read() const { return readable_ != 0; }
  bool has_primitive_field() const {
    for (const auto &f : fields_)
      if (f.attribute->getOwner() == GA_ATTRIB_PRIMITIVE)
        return true;
    return false;
  }
  void reserve(std::size_t count) {
    for (auto &f : fields_)
      if (!f.retained && f.error.empty())
        f.data.values.reserve(count);
  }
  NativeNamedAttributes(const GU_Detail &source, const GeometryRequest &request, bool corners, bool enabled) {
    if (!enabled)
      return;
    for (const auto &name : request.shader_attributes) {
      if (name == "Cd")
        continue; // NativeShadingAttribute owns the local color stream.
      const auto *a = named_attribute(source, name, corners);
      if (!a)
        continue;
      auto &f = fields_.emplace_back();
      f.attribute = a;
      f.data.name = name;
      f.data.components = a->getTupleSize();
      const auto *tuple = a->getAIFTuple();
      f.stamp = {
          a->getDataId(),    int(a->getOwner()), int(a->getStorageClass()), tuple ? int(tuple->getStorage(a)) : -1,
          a->getTupleSize(), tuple != nullptr};
      if ((a->getStorageClass() != GA_STORECLASS_FLOAT && a->getStorageClass() != GA_STORECLASS_INT) ||
          a->getTupleSize() < 1 || a->getTupleSize() > 3 || !a->getAIFTuple())
        f.error = "requires a numeric scalar, two-component or three-component tuple";
      if (f.error.empty())
        ++readable_;
    }
  }
  void reuse(const NativeNamedAttributeCache *previous, const std::vector<NamedAttribute> &values, std::size_t count) {
    if (!previous)
      return;
    for (auto &field : fields_) {
      if (field.stamp.data_id == GA_INVALID_DATAID)
        continue;
      const auto found = std::lower_bound(previous->fields.begin(), previous->fields.end(), field.data.name,
                                          [](const auto &entry, const auto &name) { return entry.name < name; });
      if (found == previous->fields.end() || found->name != field.data.name || found->stamp != field.stamp)
        continue;
      if (!found->error.empty()) {
        field.error = found->error;
        field.retained = true;
        continue;
      }
      if (!field.error.empty())
        continue;
      const auto data =
          std::find_if(values.begin(), values.end(), [&](const auto &value) { return value.name == field.data.name; });
      if (data == values.end() || data->components != field.data.components || data->values.size() != count ||
          !data->motion_values.empty() || !data->motion_complete)
        continue;
      field.data = *data;
      field.retained = true;
    }
    readable_ = std::count_if(fields_.begin(), fields_.end(),
                              [](const auto &field) { return !field.retained && field.error.empty(); });
  }
  std::shared_ptr<const NativeNamedAttributeCache> cache() const {
    if (fields_.empty())
      return {};
    auto result = std::make_shared<NativeNamedAttributeCache>();
    result->fields.reserve(fields_.size());
    for (const auto &field : fields_)
      result->fields.push_back({field.data.name, field.stamp, field.error});
    return result;
  }
  std::uint64_t fields_captured() const {
    return std::count_if(fields_.begin(), fields_.end(), [](const auto &field) { return !field.retained; });
  }
  std::uint64_t fields_reused() const { return fields_.size() - fields_captured(); }
  std::uint64_t values_read() const { return values_read_; }
  std::uint64_t values_reused() const {
    std::uint64_t result{};
    for (const auto &field : fields_)
      if (field.retained && field.error.empty())
        result += field.data.values.size();
    return result;
  }
  void read(GA_Offset vertex, GA_Offset point, GA_Offset primitive) {
    for (auto &f : fields_)
      if (!f.retained && f.error.empty()) {
        const auto owner = f.attribute->getOwner();
        const auto offset = owner == GA_ATTRIB_VERTEX      ? vertex
                            : owner == GA_ATTRIB_POINT     ? point
                            : owner == GA_ATTRIB_PRIMITIVE ? primitive
                                                           : GA_DETAIL_OFFSET;
        fpreal32 values[3]{};
        ++values_read_;
        if (offset == GA_INVALID_OFFSET ||
            !f.attribute->getAIFTuple()->get(f.attribute, offset, values, f.data.components))
          f.error = "has no unambiguous value on this geometry component";
        else if (!std::isfinite(values[0]) || !std::isfinite(values[1]) || !std::isfinite(values[2]))
          f.error = "contains a non-finite value";
        else
          f.data.values.push_back({values[0], values[1], values[2]});
        if (!f.error.empty())
          --readable_;
      }
  }
  void finish(std::vector<NamedAttribute> &result, const GeometryRequest &request,
              std::vector<SourceDiagnostic> *diagnostics) {
    for (auto &f : fields_)
      if (f.error.empty())
        result.push_back(std::move(f.data));
      else if (diagnostics)
        diagnostics->push_back({"geometry.invalid_attribute", request.sop_path, request.geometry_key,
                                "Attribute " + f.data.name + " " + f.error,
                                "Ignoring this field; retaining geometry and independent material branches", "warning",
                                f.data.name});
  }
  // Slot-addressed access for parallel converters (point_curve_adapter.cpp).
  // slot_resize() sizes a readable field once; tasks then read the attribute
  // themselves and fill disjoint slots; slot_finish() records the failure the
  // serial read() calls would have met first (code 1: no unambiguous value,
  // 2: non-finite) and the number of values those calls would have counted.
  std::size_t field_count() const { return fields_.size(); }
  bool slot_readable(std::size_t field) const { return !fields_[field].retained && fields_[field].error.empty(); }
  const GA_Attribute *slot_attribute(std::size_t field) const { return fields_[field].attribute; }
  int slot_components(std::size_t field) const { return fields_[field].data.components; }
  Float3 *slot_resize(std::size_t field, std::size_t count) {
    auto &values = fields_[field].data.values;
    values.resize(count);
    // The storage was just allocated and is unshared; the const view avoids
    // marking it exported (exported arrays are cloned on every later copy).
    return const_cast<Float3 *>(std::as_const(values).data());
  }
  void slot_finish(std::size_t field, int code, std::uint64_t reads) {
    auto &f = fields_[field];
    values_read_ += reads;
    if (!code)
      return;
    f.error = code == 1 ? "has no unambiguous value on this geometry component" : "contains a non-finite value";
    --readable_;
  }
  void read_interpolated(const GU_Detail &source, const std::vector<GA_Offset> &vertices,
                         const std::vector<float> &weights, GA_Offset primitive) {
    for (auto &f : fields_)
      if (!f.retained && f.error.empty()) {
        const auto owner = f.attribute->getOwner();
        double sum[3]{};
        const bool varying = owner == GA_ATTRIB_VERTEX || owner == GA_ATTRIB_POINT;
        for (std::size_t i = 0; i < (varying ? vertices.size() : 1); ++i) {
          const auto offset = owner == GA_ATTRIB_VERTEX      ? vertices[i]
                              : owner == GA_ATTRIB_POINT     ? source.vertexPoint(vertices[i])
                              : owner == GA_ATTRIB_PRIMITIVE ? primitive
                                                             : GA_DETAIL_OFFSET;
          fpreal32 value[3]{};
          if (!f.attribute->getAIFTuple()->get(f.attribute, offset, value, f.data.components)) {
            f.error = "has no usable value on this curve";
            break;
          }
          for (int c = 0; c < 3; ++c)
            sum[c] += double(varying ? weights[i] : 1) * value[c];
        }
        const Float3 value{float(sum[0]), float(sum[1]), float(sum[2])};
        if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
          f.error = "contains a non-finite interpolated value";
        if (f.error.empty())
          f.data.values.push_back(value);
        else
          --readable_;
      }
  }
};
} // namespace cycles_houdini
