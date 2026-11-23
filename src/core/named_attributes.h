// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "attribute_names.h"
#include "cycles_houdini/session.h"
#include "scene/attribute.h"
#include "scene/object.h"
#include <algorithm>
#include <cmath>
#include <set>

namespace cycles_houdini {
inline void validate_instance_attribute_layout(const AttributeLayout &fields,
                                               const std::vector<NamedAttribute> &geometry) {
  for (const auto &[name, components] : fields) {
    if (name.empty() || name.size() > 256 || name.find('\0') != std::string::npos || components < 1 || components > 3 ||
        std::any_of(geometry.begin(), geometry.end(), [&](const auto &field) { return field.name == name; }))
      throw std::invalid_argument("Invalid owned instance attribute declaration");
  }
}
inline void validate_uniform_attributes(const std::vector<UniformAttribute> &fields) {
  std::set<std::string> names;
  for (const auto &field : fields) {
    if (field.name.empty() || field.name.size() > 256 || field.name.find('\0') != std::string::npos ||
        !names.insert(field.name).second || field.components < 1 || field.components > 3 ||
        !std::isfinite(field.value.x) || !std::isfinite(field.value.y) || !std::isfinite(field.value.z))
      throw std::invalid_argument("Invalid owned instance attribute value");
    if (!field.motion_values.empty() && (!field.motion_complete || field.motion_values.size() < 3 ||
                                         field.motion_values.size() > 129 || !(field.motion_values.size() & 1)))
      throw std::invalid_argument("Owned instance attribute samples require complete odd 3..129 shutter values");
    for (const auto value : field.motion_values)
      if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument("Non-finite owned instance attribute sample");
  }
}
inline std::size_t named_attribute_bytes(const std::vector<NamedAttribute> &fields) {
  std::size_t bytes = 0;
  for (const auto &field : fields) {
    bytes += field.values.size() * sizeof(Float3) + field.name.size();
    for (const auto &sample : field.motion_values)
      bytes += sample.size() * sizeof(Float3);
  }
  return bytes;
}
inline void fill_uniform_attributes(ccl::Object &object, const std::vector<UniformAttribute> &fields) {
  object.attributes.clear();
  object.attribute_motion.clear();
  object.attributes.reserve(fields.size());
  object.attribute_motion.reserve(fields.size());
  for (const auto &field : fields) {
    const auto name = ccl::ustring(engine_attribute_name(field.name));
    const auto type = field.components == 1   ? ccl::TypeFloat
                      : field.components == 2 ? ccl::TypeFloat2
                                              : ccl::TypeVector;
    object.attributes.emplace_back(name, type, 1, &field.value);
    if (field.motion_values.empty())
      continue;
    const auto count = field.motion_values.size();
    // ParamValue copies packed storage. ccl::float3 is SIMD-padded and
    // cannot represent a contiguous OIIO array of scalar/float2/vec3 values.
    if (field.components == 1) {
      std::vector<float> values;
      values.reserve(count);
      for (const auto value : field.motion_values)
        values.push_back(value.x);
      object.attribute_motion.emplace_back(name, type, int(count), values.data());
    } else if (field.components == 2) {
      std::vector<Float2> values;
      values.reserve(count);
      for (const auto value : field.motion_values)
        values.push_back({value.x, value.y});
      object.attribute_motion.emplace_back(name, type, int(count), values.data());
    } else
      object.attribute_motion.emplace_back(name, type, int(count), field.motion_values.data());
  }
}
inline void validate_named_attributes(const std::vector<NamedAttribute> &fields, std::size_t count,
                                      std::size_t motion_count = 0) {
  std::set<std::string> names;
  for (const auto &field : fields) {
    if (field.name.empty() || field.name.size() > 256 || field.name.find('\0') != std::string::npos ||
        !names.insert(field.name).second || field.components < 1 || field.components > 3 ||
        field.values.size() != count)
      throw std::invalid_argument("Invalid owned shader attribute layout or size");
    for (const auto value : field.values)
      if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument("Non-finite owned shader attribute");
    if (!field.motion_values.empty() && (!field.motion_complete || field.motion_values.size() != motion_count))
      throw std::invalid_argument("Owned shader attribute samples must match geometry motion");
    for (const auto &sample : field.motion_values) {
      if (sample.size() != count)
        throw std::invalid_argument("Owned shader attribute sample size mismatch");
      for (const auto value : sample)
        if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
          throw std::invalid_argument("Non-finite owned shader attribute sample");
    }
  }
}
inline void fill_named_attributes(ccl::AttributeSet &target, const std::vector<NamedAttribute> &fields,
                                  ccl::AttributeElement element, bool subdivide = false,
                                  ccl::Geometry *geometry = nullptr) {
  for (const auto &field : fields) {
    const auto type = field.components == 1   ? ccl::TypeFloat
                      : field.components == 2 ? ccl::TypeFloat2
                                              : ccl::TypeVector;
    auto *attr = target.add(ccl::ustring(engine_attribute_name(field.name)), type, element);
    if (subdivide)
      attr->flags |= ccl::ATTR_SUBDIVIDE_SMOOTH_FVAR;
    const auto write = [&](const auto &values, int step) {
      if (field.components == 1) {
        auto *p = attr->data_for_write<float>(step);
        for (const auto v : values)
          *p++ = v.x;
      } else if (field.components == 2) {
        auto *p = attr->data_for_write<ccl::float2>(step);
        for (const auto v : values)
          *p++ = ccl::make_float2(v.x, v.y);
      } else {
        auto *p = attr->data_for_write<ccl::packed_float3>(step);
        for (const auto v : values)
          *p++ = ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z));
      }
    };
    write(field.values, 0);
    if (!field.motion_values.empty()) {
      if (!geometry)
        throw std::logic_error("Sampled attributes require their owning geometry");
      attr->add_motion(geometry);
      for (std::size_t step = 0; step < field.motion_values.size(); ++step)
        write(field.motion_values[step], int(step + 1));
    }
  }
}
} // namespace cycles_houdini
