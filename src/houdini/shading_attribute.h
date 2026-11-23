// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <GA/GA_Handle.h>
#include <cmath>

namespace cycles_houdini {
// Optional shading data is sanitized before strict engine validation. Never
// replace it with numeric magenta here: that could become a displacement input.
struct NativeShadingAttribute {
  GA_ROHandleV3 three;
  GA_ROHandleV2 two;
  bool uv{}, present{};
  GA_AttributeOwner owner{GA_ATTRIB_INVALID};
  std::optional<SourceDiagnostic> diagnostic;
  NativeShadingAttribute(const GA_Attribute *attribute, bool is_uv, const std::string &source,
                         const std::string &resource)
      : uv(is_uv), present(attribute != nullptr), source_(source), resource_(resource) {
    if (!attribute)
      return;
    owner = attribute->getOwner();
    if (attribute->getStorageClass() != GA_STORECLASS_FLOAT ||
        (attribute->getTupleSize() != 3 && !(uv && attribute->getTupleSize() == 2))) {
      fail(uv ? "uv must be a two- or three-component float attribute"
              : "Cd must be a three-component float attribute");
      return;
    }
    if (attribute->getTupleSize() == 3)
      three.bind(attribute);
    else
      two.bind(attribute);
  }
  explicit operator bool() const { return present && !diagnostic; }
  // Read without recording a failure, safe on several threads at once:
  // 0 valid, 1 no unambiguous value, 2 non-finite. fail_code(code) records
  // what read() would have recorded for that value.
  int peek(GA_Offset offset, Float3 &result) const {
    if (offset == GA_INVALID_OFFSET) {
      result = {};
      return 1;
    }
    if (three.isValid()) {
      const auto value = three.get(offset);
      result = {float(value.x()), float(value.y()), uv ? 0.f : float(value.z())};
    } else {
      const auto value = two.get(offset);
      result = {float(value.x()), float(value.y()), 0};
    }
    return std::isfinite(result.x) && std::isfinite(result.y) && std::isfinite(result.z) ? 0 : 2;
  }
  void fail_code(int code) {
    if (code == 1)
      fail(uv ? "uv has no unambiguous value on this component" : "Cd has no unambiguous value on this component");
    else if (code == 2)
      fail(uv ? "uv contains a non-finite U or V component" : "Cd contains a non-finite component");
  }
  Float3 read(GA_Offset offset) {
    Float3 result;
    const int code = peek(offset, result);
    fail_code(code);
    return result;
  }
  GA_Offset element(GA_Offset vertex, GA_Offset point, GA_Offset primitive) const {
    return owner == GA_ATTRIB_VERTEX      ? vertex
           : owner == GA_ATTRIB_POINT     ? point
           : owner == GA_ATTRIB_PRIMITIVE ? primitive
                                          : GA_DETAIL_OFFSET;
  }
  Float3 read_element(GA_Offset vertex, GA_Offset point, GA_Offset primitive) {
    return read(element(vertex, point, primitive));
  }
  Float3 checked(Float3 result) {
    if (!std::isfinite(result.x) || !std::isfinite(result.y) || !std::isfinite(result.z))
      fail(uv ? "uv contains a non-finite U or V component" : "Cd contains a non-finite component");
    return result;
  }
  template <class Buffer> void finish(Buffer &values, std::vector<SourceDiagnostic> *diagnostics) const {
    if (diagnostic) {
      values.clear();
      if (diagnostics)
        diagnostics->push_back(*diagnostic);
    }
  }

private:
  std::string source_, resource_;
  void fail(const std::string &message) {
    diagnostic = SourceDiagnostic{
        uv ? "geometry.invalid_uv" : "geometry.invalid_color", source_, resource_, message,
        "Discarding this component's invalid attribute; recovering only shader branches that require it"};
  }
};
} // namespace cycles_houdini
