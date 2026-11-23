// SPDX-License-Identifier: Apache-2.0
#include "scene_adapter.h"
#include "native_external_binding.h"
#include "named_attribute.h"
#include "point_curve_adapter.h"
#include "gaussian_splat_attribute.h"
#include "mesh_adapter.h"
#include "deformation_adapter.h"
#include "material_motion.h"
#include "volume_adapter.h"
#include "packed_adapter.h"
#include "packed_blender_reference.h"
#include "packed_fragments.h" // F3: packed fragment contents
#include "cycles_houdini/volume.h"
#include "cycles_houdini/transform.h"
#include "../core/camera_motion.h"
#include <limits>
#include <chrono>
#include <bit>
#include <cmath>
#include <algorithm>
#include <atomic>
#include <functional>
#include <cstring>
#include <cstdlib>
#include <iterator>
#include <numeric>
#include <set>
#include <xmmintrin.h>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <unordered_set>
#include <tuple>
#include <GA/GA_Handle.h>
#include <GA/GA_AIFTuple.h>
#include <GA/GA_AIFStringTuple.h>
#include <GA/GA_Iterator.h>
#include <GA/GA_ElementGroup.h>
#include <GEO/GEO_PrimPoly.h>
#include <GEO/GEO_PrimPart.h>
#include <GU/GU_Detail.h>
#include <GU/GU_DetailHandle.h>
#include <GU/GU_PrimPacked.h>
#include <GU/GU_PrimVDB.h>
#include <OBJ/OBJ_Node.h>
#include <OP/OP_Context.h>
#include <OP/OP_Director.h>
#include <SOP/SOP_Node.h>
#include <UT/UT_XXHash.h>
#include <UT/UT_ParallelUtil.h>
#include <UT/UT_Thread.h>
#include <UT/UT_String.h>

namespace cycles_houdini {
namespace {
using Clock = std::chrono::steady_clock;
double elapsed(Clock::time_point t) { return std::chrono::duration<double>(Clock::now() - t).count(); }
void retain_diagnostic(std::map<std::pair<std::string, std::string>, SourceDiagnostic> &target,
                       const SourceDiagnostic &issue) {
  const auto [found, inserted] = target.try_emplace(std::make_pair(issue.code, issue.source), issue);
  if (!inserted && (issue.code == "material.override_capacity" || issue.code == "material.stylesheet_ignored")) {
    if (found->second.message.find(issue.message) == std::string::npos)
      found->second.message += "; " + issue.message;
    if (found->second.action.find(issue.action) == std::string::npos)
      found->second.action += "; " + issue.action;
  }
}
Matrix from_houdini(const UT_Matrix4D &m) {
  Matrix result{};
  // Houdini uses row vectors and translation in row 4; the integration uses column vectors.
  for (int row = 0; row < 3; ++row)
    for (int col = 0; col < 4; ++col)
      result[row * 4 + col] = float(m(col, row));
  return result;
}
std::int64_t id(const GA_Attribute *attr) { return attr ? attr->getDataId() : GA_INVALID_DATAID; }
// Used only after shared detail IDs change, and only for mixed primitive
// domains. Camera/transform edits take the existing constant-time ID path.
// Hash the selected values before allocating/triangulating owned geometry.
// Unsupported primitive/attribute representations retain conservative capture.
class ComponentDigest {
public:
  ComponentDigest() : state_(XXH3_createState()) {
    if (!state_)
      throw std::bad_alloc();
    XXH3_128bits_reset(state_);
  }
  ~ComponentDigest() { XXH3_freeState(state_); }
  ComponentDigest(const ComponentDigest &) = delete;
  void bytes(const void *data, std::size_t size) {
    const auto *input = static_cast<const unsigned char *>(data);
    while (size) {
      const auto count = std::min(size, buffer_.size() - used_);
      std::memcpy(buffer_.data() + used_, input, count);
      used_ += count;
      input += count;
      size -= count;
      if (used_ == buffer_.size())
        flush();
    }
  }
  template <class T> void scalar(const T &value) {
    static_assert(std::is_trivially_copyable_v<T>);
    bytes(&value, sizeof(value));
  }
  void string(const std::string &value) {
    scalar(value.size());
    bytes(value.data(), value.size());
  }
  std::array<std::uint64_t, 2> finish() {
    flush();
    const auto hash = XXH3_128bits_digest(state_);
    return {hash.low64, hash.high64};
  }

private:
  void flush() {
    if (used_) {
      XXH3_128bits_update(state_, buffer_.data(), used_);
      used_ = 0;
    }
  }
  XXH3_state_t *state_{};
  std::array<unsigned char, 4096> buffer_{};
  std::size_t used_{};
};
std::optional<std::array<std::uint64_t, 2>>
component_signature(const GU_Detail &source, const NativeGeometrySelection &selection, const GeometryRequest &request,
                    const NativeMotionWindow &window, bool include_full_detail = false) {
  if ((selection.full_detail && !include_full_detail) || request.subdivision.type != SubdivisionType::None)
    return {};
  if (selection.kind != NativeGeometryKind::Mesh && selection.kind != NativeGeometryKind::Points &&
      selection.kind != NativeGeometryKind::Curves)
    return {};
  ComponentDigest digest;
  digest.scalar(selection.kind);
  digest.scalar(request.smooth);
  digest.scalar(request.material_mode);
  digest.string(request.material_key);
  digest.string(request.object_material_path);
  for (const auto &layer : request.object_material_overrides)
    digest.string(layer);
  for (const auto *layers : {&request.object_surface_overrides, &request.object_displacement_overrides}) {
    digest.scalar(layers->size());
    for (const auto &layer : *layers)
      digest.string(layer);
  }
  if (request.native_styles)
    digest.string(request.native_styles->signature);
  if (request.native_fallback) {
    digest.string(request.native_fallback->key);
    digest.string(request.native_fallback->identity);
  }
  digest.scalar(request.point_radius);
  digest.scalar(request.point_render_mode);
  digest.scalar(request.point_splat_default_material);
  digest.scalar(request.curve_width);
  digest.scalar(request.curve_segments);
  digest.scalar(request.motion);
  digest.scalar(window.enabled);
  if (window.enabled) {
    digest.scalar(window.deltas.size());
    for (const auto delta : window.deltas)
      digest.scalar(delta);
  }
  const auto points = selection.point_range(source), primitives = selection.primitive_range(source),
             vertices = selection.vertex_range(source);
  digest.scalar(points.getEntries());
  digest.scalar(primitives.getEntries());
  for (GA_Iterator it(points); !it.atEnd(); ++it) {
    // Global indices can affect index-based particle/curve identities.
    digest.scalar(source.pointIndex(*it));
  }
  // getPos3 and the owned geometry converters use three float components.
  // Read that same representation in bounded public-API blocks.
  const auto *positions = source.getP()->getAIFTuple();
  if (!positions)
    return {};
  std::array<fpreal32, 512 * 3> position_values;
  GA_Offset begin, end;
  for (GA_Iterator it(points); it.blockAdvance(begin, end);) {
    for (auto first = begin; first < end;) {
      const auto last = std::min(end, first + 512);
      const GA_Range block(source.getPointMap(), first, last);
      if (!positions->getRange(source.getP(), block, position_values.data(), 0, 3))
        return {};
      digest.bytes(position_values.data(), std::size_t(last - first) * 3 * sizeof(fpreal32));
      first = last;
    }
  }
  for (GA_Iterator it(primitives); !it.atEnd(); ++it) {
    const auto *primitive = source.getPrimitive(*it);
    if (const auto *polygon = dynamic_cast<const GEO_PrimPoly *>(primitive))
      digest.scalar(polygon->isClosed());
    else if (!dynamic_cast<const GEO_PrimParticle *>(primitive))
      return {}; // Polygon soups need a face-aware signature.
    digest.scalar(source.primitiveIndex(*it));
    digest.scalar(primitive->getVertexCount());
    for (exint vertex = 0; vertex < primitive->getVertexCount(); ++vertex)
      digest.scalar(source.pointIndex(primitive->getPointOffset(vertex)));
  }
  const auto attribute_values = [&](const GA_Attribute *attribute, const GA_Range &range) {
    digest.scalar(attribute != nullptr);
    if (!attribute)
      return true;
    const auto storage = attribute->getStorageClass();
    const auto count = attribute->getTupleSize();
    digest.scalar(storage);
    digest.scalar(count);
    digest.scalar(range.getEntries());
    if (count <= 0 || count > 16)
      return false;
    if (storage == GA_STORECLASS_STRING) {
      const auto *values = attribute->getAIFStringTuple();
      if (!values)
        return false;
      for (GA_Iterator it(range); !it.atEnd(); ++it)
        for (int component = 0; component < count; ++component) {
          const auto *value = values->getString(attribute, *it, component);
          digest.string(value ? value : "");
        }
    } else if (storage == GA_STORECLASS_FLOAT || storage == GA_STORECLASS_INT) {
      const auto *values = attribute->getAIFTuple();
      if (!values)
        return false;
      // Public bulk tuple access preserves the range's iteration order.
      // Bound scratch space independently of the scene size.
      std::vector<fpreal64> floating;
      std::vector<int64> integer;
      if (storage == GA_STORECLASS_FLOAT)
        floating.resize(512 * count);
      else
        integer.resize(512 * count);
      GA_Offset begin, end;
      for (GA_Iterator it(range); it.blockAdvance(begin, end);) {
        for (auto first = begin; first < end;) {
          const auto last = std::min(end, first + 512);
          const GA_Range block(attribute->getIndexMap(), first, last);
          const auto length = std::size_t(last - first) * std::size_t(count);
          if (storage == GA_STORECLASS_FLOAT) {
            if (!values->getRange(attribute, block, floating.data(), 0, count))
              return false;
            digest.bytes(floating.data(), length * sizeof(fpreal64));
          } else {
            if (!values->getRange(attribute, block, integer.data(), 0, count))
              return false;
            digest.bytes(integer.data(), length * sizeof(int64));
          }
          first = last;
        }
      }
    } else
      return false;
    return true;
  };
  // Mirror each converter's consumed/validated domains. Shared mesh/hair
  // points remain in both signatures; unrelated primitive types' attributes
  // must not force another geometry conversion.
  const auto point_attribute = [&](const char *name) {
    return attribute_values(source.findPointAttribute(name), points);
  };
  const auto primitive_attribute = [&](const char *name) {
    return attribute_values(source.findPrimitiveAttribute(name), primitives);
  };
  const auto corner_attribute = [&](const char *name) {
    return point_attribute(name) && attribute_values(source.findVertexAttribute(name), vertices);
  };
  digest.scalar(std::count_if(request.shader_attributes.begin(), request.shader_attributes.end(),
                              [](const auto &name) { return name != "Cd"; }));
  digest.scalar(request.pointiness);
  for (const auto &name : request.shader_attributes) {
    if (name == "Cd")
      continue; // Dedicated color domains are fingerprinted below.
    digest.string(name);
    if (!corner_attribute(name.c_str()) || !primitive_attribute(name.c_str()) ||
        !attribute_values(source.findGlobalAttribute(name.c_str()), source.getGlobalRange()))
      return {};
  }
  if (selection.kind == NativeGeometryKind::Mesh) {
    for (const auto *name : {"N", "uv", "Cd"})
      if (!corner_attribute(name))
        return {};
  } else if (selection.kind == NativeGeometryKind::Points) {
    digest.string(gaussian_splat_color_identity(source, request));
    for (const auto *name : {"pscale", "Cd", "uv", "id", "name"})
      if (!point_attribute(name))
        return {};
    const auto splat_format = native_gaussian_splat_format(source, request);
    digest.scalar(splat_format);
    bool splat_values = true;
    gaussian_splat_fields(source, splat_format, [&](const char *name, const GA_Attribute *attribute) {
      if (!splat_values)
        return;
      digest.string(name);
      if (!attribute) {
        splat_values = attribute_values(nullptr, points);
        return;
      }
      digest.scalar(attribute->getOwner());
      const auto &range = attribute->getOwner() == GA_ATTRIB_POINT       ? points
                          : attribute->getOwner() == GA_ATTRIB_PRIMITIVE ? primitives
                                                                         : source.getGlobalRange();
      splat_values = attribute_values(attribute, range);
    });
    if (!splat_values)
      return {};
  } else {
    for (const auto *name : {"uv", "Cd", "width"})
      if (!corner_attribute(name))
        return {};
    for (const auto *name : {"width", "id", "name"})
      if (!primitive_attribute(name))
        return {};
    if (!attribute_values(source.findGlobalAttribute("width"), source.getGlobalRange()))
      return {};
  }
  if (!primitive_attribute("Cd") || !attribute_values(source.findGlobalAttribute("Cd"), source.getGlobalRange()))
    return {};
  if (window.enabled && attribute_motion(request.motion)) {
    if (!point_attribute("v"))
      return {};
    // Acceleration mode also reads point accel (acceleration_attribute):
    // without it here, an accel-only change was answered with retained geometry.
    if (request.motion == NativeGeometryMotion::Acceleration && !point_attribute("accel"))
      return {};
  }
  // Material inspection also enforces unsupported-owner/override diagnostics,
  // so retain every currently validated material domain conservatively.
  for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"})
    if (!corner_attribute(name) || !primitive_attribute(name) ||
        !attribute_values(source.findGlobalAttribute(name), source.getGlobalRange()))
      return {};
  return digest.finish();
}
// Content of a leaf packed prototype as the converters read it: every public
// numeric or string tuple attribute of every owner, the subdivision hole group
// and polygon/particle topology. Array and dictionary attributes (no tuple
// interface, e.g. a detail `visualizer` string array) are never converted, so
// only their names count. Empty for any other primitive type (packed, volumes,
// soups, NURBS...): those keep their GU detail identity.
std::optional<std::array<std::uint64_t, 2>> prototype_content(const GU_Detail &source) {
  // Style sheets can read further fields (stylesheet_evaluation.cpp).
  for (const auto owner : {GA_ATTRIB_POINT, GA_ATTRIB_VERTEX, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
    if (source.findAttribute(owner, "material_stylesheet"))
      return {};
  ComponentDigest digest;
  digest.string("packed_prototype_content/1");
  digest.scalar(source.getNumPoints());
  digest.scalar(source.getNumVertices());
  digest.scalar(source.getNumPrimitives());
  for (GA_Iterator it(source.getPrimitiveRange()); !it.atEnd(); ++it) {
    const auto *primitive = source.getPrimitive(*it);
    if (const auto *polygon = dynamic_cast<const GEO_PrimPoly *>(primitive)) {
      digest.scalar(0);
      digest.scalar(polygon->isClosed());
    } else if (dynamic_cast<const GEO_PrimParticle *>(primitive))
      digest.scalar(1);
    else
      return {};
    digest.scalar(primitive->getVertexCount());
    for (exint vertex = 0; vertex < primitive->getVertexCount(); ++vertex)
      digest.scalar(source.pointIndex(primitive->getPointOffset(vertex)));
  }
  const auto values = [&](const GA_Attribute &attribute, const GA_Range &range) {
    const auto storage = attribute.getStorageClass();
    const auto count = attribute.getTupleSize();
    digest.scalar(storage);
    digest.scalar(count);
    if (count <= 0 || count > 64)
      return false;
    if (storage == GA_STORECLASS_STRING) {
      const auto *strings = attribute.getAIFStringTuple();
      if (!strings) {
        digest.string("unconverted");
        return true;
      }
      for (GA_Iterator it(range); !it.atEnd(); ++it)
        for (int component = 0; component < count; ++component) {
          const auto *value = strings->getString(&attribute, *it, component);
          digest.string(value ? value : "");
        }
      return true;
    }
    const auto *tuple = attribute.getAIFTuple();
    if ((storage != GA_STORECLASS_FLOAT && storage != GA_STORECLASS_INT) || !tuple) {
      digest.string("unconverted");
      return true;
    }
    std::vector<fpreal64> floating(storage == GA_STORECLASS_FLOAT ? 512 * count : 0);
    std::vector<int64> integer(storage == GA_STORECLASS_INT ? 512 * count : 0);
    GA_Offset begin, end;
    for (GA_Iterator it(range); it.blockAdvance(begin, end);)
      for (auto first = begin; first < end;) {
        const auto last = std::min(end, first + 512);
        const GA_Range block(attribute.getIndexMap(), first, last);
        const auto length = std::size_t(last - first) * std::size_t(count);
        if (storage == GA_STORECLASS_FLOAT) {
          if (!tuple->getRange(&attribute, block, floating.data(), 0, count))
            return false;
          digest.bytes(floating.data(), length * sizeof(fpreal64));
        } else {
          if (!tuple->getRange(&attribute, block, integer.data(), 0, count))
            return false;
          digest.bytes(integer.data(), length * sizeof(int64));
        }
        first = last;
      }
    return true;
  };
  const std::pair<const GA_AttributeDict *, GA_Range> owners[] = {
      {&source.pointAttribs(), source.getPointRange()},
      {&source.vertexAttribs(), source.getVertexRange()},
      {&source.primitiveAttribs(), source.getPrimitiveRange()},
      {&source.attribs(), source.getGlobalRange()}};
  for (const auto &[dictionary, range] : owners) {
    std::map<std::string, const GA_Attribute *> sorted;
    for (GA_AttributeDict::iterator it = dictionary->begin(GA_SCOPE_PUBLIC); !it.atEnd(); ++it)
      sorted.emplace(it.attrib()->getName().toStdString(), it.attrib());
    digest.scalar(sorted.size());
    for (const auto &[name, attribute] : sorted) {
      digest.string(name);
      if (!values(*attribute, range))
        return {};
    }
  }
  const auto *holes = source.findPrimitiveGroup("subdivision_hole");
  digest.scalar(holes != nullptr);
  if (holes)
    for (GA_Iterator it(source.getPrimitiveRange()); !it.atEnd(); ++it)
      digest.scalar(holes->contains(*it));
  return digest.finish();
}
Matrix multiply(const Matrix &a, const Matrix &b) {
  Matrix result{};
  for (int r = 0; r < 3; ++r) {
    for (int c = 0; c < 3; ++c)
      for (int k = 0; k < 3; ++k)
        result[r * 4 + c] += a[r * 4 + k] * b[k * 4 + c];
    result[r * 4 + 3] = a[r * 4 + 3];
    for (int k = 0; k < 3; ++k)
      result[r * 4 + 3] += a[r * 4 + k] * b[k * 4 + 3];
  }
  return result;
}
// Converter-owned per-instance work on Houdini's TBB pool. Each task writes
// only its own output slots, and runs with the calling thread's SSE control
// word (rounding, flush-to-zero), so every value is bitwise the serial one.
class ScopedFloatControl {
public:
  explicit ScopedFloatControl(unsigned int control) : saved_(_mm_getcsr()) { _mm_setcsr(control); }
  ~ScopedFloatControl() { _mm_setcsr(saved_); }
  ScopedFloatControl(const ScopedFloatControl &) = delete;
  ScopedFloatControl &operator=(const ScopedFloatControl &) = delete;

private:
  unsigned int saved_;
};
// CYCLES_HOUDINI_SERIAL_CONVERSION=1 runs the same slot work on the calling
// thread only (equivalence tests compare both schedules).
bool serial_conversion() {
  const auto *value = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  return value && std::strcmp(value, "1") == 0;
}
template <class Body> void parallel_slots(std::size_t count, std::size_t grain, const Body &body) {
  if (count <= grain || serial_conversion()) {
    body(std::size_t{0}, count);
    return;
  }
  const auto control = _mm_getcsr();
  UTparallelFor(UT_BlockedRange<std::size_t>(0, count, grain), [&](const UT_BlockedRange<std::size_t> &range) {
    ScopedFloatControl scope(control);
    body(range.begin(), range.end());
  });
}
// Keys sorted here are unique (callers reject equal neighbours), so any
// correct sort yields the same order.
template <class Compare> void parallel_sort(std::vector<std::size_t> &order, const Compare &less) {
  if (order.size() < 4096 || serial_conversion())
    std::sort(order.begin(), order.end(), less);
  else
    UTparallelSort(order.begin(), order.end(), less);
}
std::uint32_t random_id(const std::string &key) {
  std::uint32_t value = 2166136261u;
  for (unsigned char c : key) {
    value ^= c;
    value *= 16777619u;
  }
  return value;
}
// Blender's hash_uint2 (= BLI_hash_int_2d) and hash_string, ported from Blender v5.3.0
// intern/cycles/util/hash.h (Apache-2.0, SPDX-FileCopyrightText: 2011-2022 Blender
// Foundation). Blender builds with unsigned char, so every byte adds 0..255.
std::uint32_t blender_hash_uint2(std::uint32_t kx, std::uint32_t ky) {
  const auto rot = [](std::uint32_t x, int k) { return (x << k) | (x >> (32 - k)); };
  std::uint32_t a, b, c;
  a = b = c = 0xdeadbeefu + (2u << 2) + 13u;
  b += ky;
  a += kx;
  c ^= b;
  c -= rot(b, 14);
  a ^= c;
  a -= rot(c, 11);
  b ^= a;
  b -= rot(a, 25);
  c ^= b;
  c -= rot(b, 16);
  a ^= c;
  a -= rot(c, 4);
  b ^= a;
  b -= rot(a, 14);
  c ^= b;
  c -= rot(b, 24);
  return c;
}
std::uint32_t blender_hash_string(const std::string &text) {
  std::uint32_t value = 0;
  for (unsigned char c : text)
    value = value * 37u + c;
  return value;
}
// Link members: Blender's per-object values combined with the Houdini object's
// (docs/dev/blend-import.md "Per-object values"). Ray visibility is ANDed (Houdini can hide
// more, never un-hide), holdout and shadow catcher are ORed, the Houdini pass index wins
// when it is not 0, colour and alpha are Blender's (Houdini has no object colour). `first`:
// this object is the first Houdini placement of that Blender object or instance under its
// Houdini root and keeps Blender's random number exactly; every further placement XORs it
// with a hash of its own object key, as Blender does with the root object of an instance.
void apply_blender_object(ObjectData &data, const GeometryRequest &member, bool first) {
  if (!member.blender_object)
    return;
  const auto &blender = *member.blender_object;
  data.random_id = first ? blender.random_id : blender.random_id ^ blender_hash_uint2(blender_hash_string(data.key), 0);
  data.visibility &= blender.visibility;
  data.properties.holdout = data.properties.holdout || blender.holdout;
  data.properties.shadow_catcher = data.properties.shadow_catcher || blender.shadow_catcher;
  if (data.properties.pass_index == 0)
    data.properties.pass_index = blender.pass_index;
  data.properties.color = blender.color;
  data.properties.alpha = blender.alpha;
}
void velocity_fingerprint(std::vector<std::int64_t> &fingerprint, const GU_Detail &source,
                          const GeometryRequest &request, const NativeMotionWindow &window) {
  const bool enabled = window.enabled && attribute_motion(request.motion);
  const bool acceleration = enabled && request.motion == NativeGeometryMotion::Acceleration;
  fingerprint.push_back(acceleration ? 2 : enabled ? 1 : 0);
  if (enabled) {
    fingerprint.push_back(id(source.findPointAttribute("v")));
    if (acceleration)
      fingerprint.push_back(id(source.findPointAttribute("accel")));
    fingerprint.push_back(static_cast<std::int64_t>(window.times.size()));
    for (const auto delta : window.deltas)
      fingerprint.push_back(std::bit_cast<std::int64_t>(delta));
  }
}
bool usable_velocity(const GU_Detail &source, const GeometryRequest &request, const NativeGeometrySelection &selection,
                     const NativeMotionWindow &window, std::string &warning) {
  warning.clear();
  const auto *attribute = source.findPointAttribute("v");
  if (!window.enabled || !attribute_motion(request.motion) || !attribute)
    return false;
  const auto reject = [&](const char *reason) {
    warning = request.sop_path + " (" + geometry_kind_name(selection.kind) + "): velocity blur disabled; " + reason +
              ". Rendering current geometry.";
    return false;
  };
  if (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != 3)
    return reject("v must be a three-component float attribute");
  GA_ROHandleV3 velocity(attribute);
  if (!velocity.isValid())
    return reject("v cannot be read as a vector");
  // Validate only this geometry partition. Bad hair velocities must not
  // disable valid mesh motion in the same OBJ. Never read changing SOP data.
  for (GA_Iterator it(selection.point_range(source)); !it.atEnd(); ++it) {
    const auto value = velocity.get(*it);
    const auto position = source.getPos3(*it);
    for (int axis = 0; axis < 3; ++axis) {
      const float v = float(value[axis]);
      if (!std::isfinite(v))
        return reject("v must contain finite values");
      for (const double delta : {window.deltas.front(), window.deltas.back()})
        if (!std::isfinite(float(float(position[axis]) + v * delta)))
          return reject("velocity produces an unrepresentable shutter position");
    }
  }
  return true;
}
// One velocity- or acceleration-mode position at `delta` seconds from the
// frame: P + v dt + 1/2 accel dt^2. The paths of points, curves, meshes and
// packed/point instances all go through here (accel is 0 in Velocity mode).
inline double velocity_position(double position, double velocity, double delta, double acceleration = 0) {
  return position + velocity * delta + 0.5 * acceleration * delta * delta;
}
// Acceleration mode: the frame's point `accel` (units per second squared), or
// null when it is absent, not a three-component float attribute, or another
// mode is active. A wrong attribute type is reported once in `warning`.
const GA_Attribute *acceleration_attribute(const GU_Detail &source, const GeometryRequest &request,
                                           const NativeMotionWindow &window, std::string *warning = nullptr) {
  if (!window.enabled || request.motion != NativeGeometryMotion::Acceleration)
    return nullptr;
  const auto *attribute = source.findPointAttribute("accel");
  if (attribute && (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != 3)) {
    if (warning && warning->empty())
      *warning = request.sop_path + ": accel must be a three-component float point attribute; using v only.";
    return nullptr;
  }
  return attribute;
}
// Velocity motion is linear, so `linear` publishes only shutter open, centre
// and close, as Blender's attr_create_motion_from_velocity does. A curved
// (accelerated) path publishes every configured step. Meshes keep the
// configured count: shutter-sampled displacement needs matching geometry steps
// (core/session_validation.cpp). Returns false when `accelerations` was given
// but unusable (wrong size, non-finite position): v alone is then used.
bool velocity_motion(std::vector<Float3> &positions, GeometryMotion &motion, const std::vector<Float3> &velocities,
                     const NativeMotionWindow &window, bool linear = false,
                     const std::vector<Float3> *accelerations = nullptr) {
  if (positions.empty())
    return true;
  if (positions.size() != velocities.size())
    throw std::invalid_argument("Velocity/position correspondence does not match");
  for (const auto &v : velocities)
    if (!std::isfinite(v.x) || !std::isfinite(v.y) || !std::isfinite(v.z))
      throw std::invalid_argument("Point v must contain finite values");
  bool accepted = true;
  const std::vector<Float3> *accel = accelerations && !accelerations->empty() ? accelerations : nullptr;
  if (accel) {
    accepted = accel->size() == positions.size();
    for (std::size_t i = 0; accepted && i < positions.size(); ++i)
      for (const double delta : {window.deltas.front(), window.deltas.back()}) {
        const auto &p = positions[i], &v = velocities[i], &a = (*accel)[i];
        accepted &= std::isfinite(float(velocity_position(p.x, v.x, delta, a.x))) &&
                    std::isfinite(float(velocity_position(p.y, v.y, delta, a.y))) &&
                    std::isfinite(float(velocity_position(p.z, v.z, delta, a.z)));
      }
    if (!accepted)
      accel = nullptr;
  }
  const auto moved = [&](double delta) {
    std::vector<Float3> result;
    result.reserve(positions.size());
    for (std::size_t i = 0; i < positions.size(); ++i) {
      const Float3 a = accel ? (*accel)[i] : Float3{};
      result.push_back({float(velocity_position(positions[i].x, velocities[i].x, delta, a.x)),
                        float(velocity_position(positions[i].y, velocities[i].y, delta, a.y)),
                        float(velocity_position(positions[i].z, velocities[i].z, delta, a.z))});
    }
    return result;
  };
  std::vector<double> deltas = window.deltas;
  if (linear && !accel && deltas.size() > 3)
    deltas = {window.deltas.front(), window.deltas[window.deltas.size() / 2], window.deltas.back()};
  for (std::size_t i = 0; i < deltas.size(); ++i)
    if (i != deltas.size() / 2)
      motion.positions.push_back(moved(deltas[i]));
  auto centre = moved(deltas[deltas.size() / 2]);
  positions = std::move(centre);
  return accepted;
}
// Pure assembly from owned instance metadata. Leaf batches run this after
// handoff; only nested containers need it during host hierarchy acquisition.
// Velocity mode moves a packed primitive or point instance by its own point's
// v (units per second) and, when present, rotates it by that point's w (an
// angular velocity vector, radians per second, right-handed about w/|w|) about
// the point position, i.e. the packed pivot. Both are in the space of the
// detail that holds the packed point; dt is the step's offset from the frame.
// Acceleration mode adds 1/2 accel dt^2 of that point to the translation; the
// spin stays at the constant rate w.
std::vector<Matrix> packed_velocity_samples(const Matrix &parent, const std::vector<Matrix> *parent_motion,
                                            const Matrix &instance, const std::optional<Float3> &velocity,
                                            const NativeMotionWindow &window, const std::vector<Matrix> &root_motion,
                                            bool &invalid, const std::optional<Float3> &angular = {},
                                            const Float3 &pivot = {}, const std::optional<Float3> &acceleration = {}) {
  invalid = false;
  if (!parent_motion && !velocity && !angular && !acceleration)
    return {};
  if (root_motion.size() != window.deltas.size() || (parent_motion && parent_motion->size() != window.deltas.size()))
    throw std::logic_error("Packed velocity samples do not match their owned shutter window");
  std::vector<Matrix> result;
  result.reserve(window.deltas.size());
  const auto sample = [&](bool use_velocity) {
    result.clear();
    for (std::size_t step = 0; step < window.deltas.size(); ++step) {
      auto matrix = instance;
      const double delta = window.deltas[step];
      if (use_velocity && angular) {
        const double wx = angular->x, wy = angular->y, wz = angular->z;
        const double rate = std::sqrt(wx * wx + wy * wy + wz * wz), angle = rate * delta;
        if (rate > 0 && angle != 0) {
          const double x = wx / rate, y = wy / rate, z = wz / rate, c = std::cos(angle), s = std::sin(angle), t = 1 - c;
          const double rotation[3][3] = {{t * x * x + c, t * x * y - s * z, t * x * z + s * y},
                                         {t * x * y + s * z, t * y * y + c, t * y * z - s * x},
                                         {t * x * z - s * y, t * y * z + s * x, t * z * z + c}};
          const double offset[3] = {double(instance[3]) - pivot.x, double(instance[7]) - pivot.y,
                                    double(instance[11]) - pivot.z};
          const double centre[3] = {pivot.x, pivot.y, pivot.z};
          for (int r = 0; r < 3; ++r) {
            for (int c2 = 0; c2 < 3; ++c2) {
              double value = 0;
              for (int k = 0; k < 3; ++k)
                value += rotation[r][k] * double(instance[k * 4 + c2]);
              matrix[r * 4 + c2] = float(value);
            }
            double moved = centre[r];
            for (int k = 0; k < 3; ++k)
              moved += rotation[r][k] * offset[k];
            matrix[r * 4 + 3] = float(moved);
          }
        }
      }
      if (use_velocity && (velocity || acceleration)) {
        const auto v = velocity.value_or(Float3{}), a = acceleration.value_or(Float3{});
        matrix[3] = float(velocity_position(matrix[3], v.x, delta, a.x));
        matrix[7] = float(velocity_position(matrix[7], v.y, delta, a.y));
        matrix[11] = float(velocity_position(matrix[11], v.z, delta, a.z));
      }
      result.push_back(multiply(parent_motion ? parent_motion->at(step) : parent, matrix));
    }
  };
  sample(bool(velocity) || bool(angular) || bool(acceleration));
  // Cycles decomposes each step and slerps between neighbouring steps along
  // the shortest arc (util/transform.cpp transform_motion_decompose): half a
  // turn or more between two steps would reverse or lose the spin.
  bool too_fast = false;
  if (angular && window.deltas.size() > 1) {
    const double rate =
        std::sqrt(double(angular->x) * angular->x + double(angular->y) * angular->y + double(angular->z) * angular->z);
    const double interval = (window.deltas.back() - window.deltas.front()) / double(window.deltas.size() - 1);
    too_fast = !(rate * interval < 3.14159265358979323846 * 0.999);
  }
  if (velocity || angular || acceleration)
    for (std::size_t step = 0; step < result.size(); ++step) {
      const auto world = multiply(root_motion[step], result[step]);
      if (too_fast ||
          !std::all_of(result[step].begin(), result[step].end(), [](float value) { return std::isfinite(value); }) ||
          !std::all_of(world.begin(), world.end(), [](float value) { return std::isfinite(value); })) {
        invalid = true;
        if (parent_motion)
          sample(false);
        else
          result.clear();
        break;
      }
    }
  return result;
}
} // namespace
NativeMotionWindow native_motion_window(double time, const NativeMotionSettings &settings) {
  if (!std::isfinite(time) || !std::isfinite(settings.frames_per_second) || settings.frames_per_second <= 0 ||
      !std::isfinite(settings.shutter_frames) || settings.shutter_frames < 0 || settings.shutter_frames > 1 ||
      !std::isfinite(settings.shutter_offset) || settings.shutter_offset < -1 || settings.shutter_offset > 1)
    throw std::invalid_argument(
        "Native motion requires finite time, positive FPS, shutter in [0,1] frames and offset in [-1,1]");
  if (settings.samples < 3 || settings.samples > 129 || settings.samples % 2 != 1)
    throw std::invalid_argument("Native motion sample count must be odd, from 3 to 129");
  NativeMotionWindow result;
  result.nominal = result.centre = time;
  result.shutter_frames = settings.shutter_frames;
  result.vectors = settings.vectors;
  if (settings.vectors) {
    result.enabled = true;
    result.shutter_frames = 2;
    result.deltas = {-1 / settings.frames_per_second, 0, 1 / settings.frames_per_second};
    result.times = {time + result.deltas[0], time, time + result.deltas[2]};
    return result;
  }
  result.enabled = settings.enabled && settings.shutter_frames > 0;
  if (!result.enabled) {
    result.times = {time};
    result.deltas = {0};
    return result;
  }
  const double duration = settings.shutter_frames / settings.frames_per_second;
  result.centre = time + settings.shutter_offset * duration * 0.5;
  if (!std::isfinite(duration) || !std::isfinite(result.centre + duration) || !std::isfinite(result.centre - duration))
    throw std::invalid_argument("Native shutter interval exceeds the supported time range");
  for (int i = 0; i < settings.samples; ++i) {
    const double delta = duration * (settings.shutter_offset * 0.5 + double(i) / (settings.samples - 1) - 0.5);
    result.deltas.push_back(delta);
    result.times.push_back(time + delta);
  }
  // Keep the centre bit-identical to the base time used by all adapters.
  result.times[settings.samples / 2] = result.centre;
  return result;
}
Matrix capture_object_transform(const std::string &path, double time) {
  auto *node = OPgetDirector()->findOBJNode(path.c_str());
  if (!node)
    throw std::invalid_argument("Missing OBJ node: " + path);
  OP_Context context(time);
  UT_Matrix4D matrix;
  if (!node->getWorldTransform(matrix, context))
    throw std::runtime_error("Could not evaluate transform: " + path);
  return from_houdini(matrix);
}
Matrix capture_sop_object_transform(const std::string &path, double time) {
  auto *sop = OPgetDirector()->findSOPNode(path.c_str());
  if (!sop)
    throw std::invalid_argument("Missing SOP: " + path);
  auto *parent = sop->getCreator();
  while (parent && !dynamic_cast<OBJ_Node *>(parent))
    parent = parent->getCreator();
  return parent ? capture_object_transform(parent->getFullPath().toStdString(), time) : identity_matrix;
}
static PanoramaSettings camera_panorama_source(OBJ_Node *camera, double time) {
  const auto text = [&](const char *name, const std::string &fallback) {
    if (!camera->getParmPtr(name))
      return fallback;
    UT_String value;
    camera->evalString(value, name, 0, time);
    return value.toStdString();
  };
  const auto value = [&](const std::string &name, double fallback) {
    return camera->getParmPtr(name.c_str()) ? camera->evalFloat(name.c_str(), 0, time) : fallback;
  };
  constexpr double degrees = 3.14159265358979323846 / 180;
  PanoramaSettings p;
  p.type = text("cycles_panorama_type", "fisheye_equisolid");
  if (p.type == "equirectangular") {
    p.latitude_min = value("cycles_latitude_min", -90) * degrees;
    p.latitude_max = value("cycles_latitude_max", 90) * degrees;
    p.longitude_min = value("cycles_longitude_min", -180) * degrees;
    p.longitude_max = value("cycles_longitude_max", 180) * degrees;
  } else if (p.type == "central_cylindrical") {
    p.cylindrical_u_min = value("cycles_cylindrical_u_min", -180) * degrees;
    p.cylindrical_u_max = value("cycles_cylindrical_u_max", 180) * degrees;
    const double radius = value("cycles_cylindrical_radius", 1),
                 divisor = std::isfinite(radius) && radius > 0 ? radius : std::numeric_limits<double>::quiet_NaN();
    p.cylindrical_v_min = value("cycles_cylindrical_v_min", -1) / divisor;
    p.cylindrical_v_max = value("cycles_cylindrical_v_max", 1) / divisor;
  } else if (p.type == "fisheye_equidistant" || p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
    p.fisheye_fov = value("cycles_fisheye_fov", 180) * degrees;
    if (p.type == "fisheye_equisolid")
      p.fisheye_lens = value("cycles_fisheye_lens", p.fisheye_lens);
    if (p.type == "fisheye_lens_polynomial")
      for (int i = 0; i < 5; ++i)
        p.polynomial[i] = value("cycles_fisheye_k" + std::to_string(i), p.polynomial[i]);
    if (p.type != "fisheye_equidistant") {
      p.sensor_fit = text("cycles_sensor_fit", "auto");
      p.sensor_height = value("cycles_sensor_height", 24);
      p.sensor_width = value("cycles_sensor_width", 36);
    }
  }
  return p; // The resolver holds discrete fit before selecting active size.
}
static CameraSourceSample camera_source_sample(const std::string &path, double time, int width, int height) {
  auto *camera = OPgetDirector()->findOBJNode(path.c_str());
  if (!camera || !camera->getParmPtr("focal") || !camera->getParmPtr("aperture"))
    throw std::invalid_argument("Missing or unsupported native camera: " + path);
  CameraSourceSample source;
  auto &result = source.value;
  result.transform = capture_object_transform(path, time);
  for (int row = 0; row < 3; ++row)
    result.transform[row * 4 + 2] *= -1; // Houdini -Z camera to Cycles +Z.
  const double focal = camera->evalFloat("focal", 0, time), aperture = camera->evalFloat("aperture", 0, time);
  const double aspect = double(width) / height;
  // compute_auto_viewplane defines FOV on the shorter dimension.
  result.short_axis_fov = std::isfinite(focal) && std::isfinite(aperture) && focal > 0 && aperture > 0
                              ? float(2 * std::atan(aperture / (2 * focal * std::max(1.0, aspect))))
                              : std::numeric_limits<float>::quiet_NaN();
  if (camera->getParmPtr("near"))
    result.near_clip = float(camera->evalFloat("near", 0, time));
  if (camera->getParmPtr("far"))
    result.far_clip = float(camera->evalFloat("far", 0, time));
  if (camera->getParmPtr("projection")) {
    source.projection = camera->evalInt("projection", 0, time);
    result.orthographic = source.projection == 1;
  }
  if (camera->getParmPtr("orthowidth"))
    result.ortho_scale = float(camera->evalFloat("orthowidth", 0, time) / aspect);
  if (camera->getParmPtr("cycles_projection")) {
    UT_String projection;
    camera->evalString(projection, "cycles_projection", 0, time);
    if (projection == "panorama") {
      source.projection = 2;
      result.orthographic = false;
      result.panorama = camera_panorama_source(camera, time);
    } else if (projection != "native")
      source.projection = -1;
  }
  return source;
}
CameraData capture_camera(const std::string &path, double time, int width, int height, float pixel_aspect) {
  return resolve_camera_source(camera_source_sample(path, time, width, height), {}, path, 0, false, width, height,
                               pixel_aspect);
}
CameraData capture_camera(const std::string &path, const NativeMotionWindow &window, int width, int height,
                          float pixel_aspect) {
  const auto nominal = camera_source_sample(path, window.nominal, width, height);
  std::vector<CameraSourceSample> samples;
  if (window.enabled) {
    samples.reserve(window.times.size());
    for (const auto time : window.times)
      samples.push_back(camera_source_sample(path, time, width, height));
  }
  return resolve_camera_source(nominal, samples, path, float(window.shutter_frames), window.vectors, width, height,
                               pixel_aspect);
}
struct NativePackedGroups {
  static constexpr auto missing = std::numeric_limits<std::size_t>::max();
  struct Group {
    std::size_t representative{}, count{};
    std::string stylesheet;
  };
  std::shared_ptr<const NativePackedInstances> rows;
  std::vector<Group> groups;
  std::vector<std::size_t> row_groups;
  std::size_t unavailable{}, first_unavailable{missing}, invalid_velocity{}, first_velocity{missing}, index_ids{};
  bool procedural{}, ambiguous{};
};
// Source-only grouping is resumable. The structural routing owner keeps every
// token in the key alive; no material callback or native attribute handle is
// retained. Each host turn still resolves the current graphs for the groups.
struct NativePackedGroupingCache {
  using GroupKey = std::tuple<std::int64_t, NativeMaterialAssignment, std::string, std::vector<std::uintptr_t>, int>;
  struct Work {
    std::shared_ptr<NativePackedGroups> result = std::make_shared<NativePackedGroups>();
    std::shared_ptr<const NativeMaterialMotionPoses> routes;
    std::map<GroupKey, std::size_t> groups;
    std::size_t next{};
    bool done{};
  };
  std::map<std::vector<std::uintptr_t>, Work> work;
  std::uint64_t capture{};
  void clear() {
    work.clear();
    capture = 0;
  }
  std::shared_ptr<const NativePackedGroups> prepare(const GU_Detail &source, const GeometryRequest &request,
                                                    const std::shared_ptr<const NativePackedInstances> &rows,
                                                    const std::vector<NativeMaterialRoute> &parent,
                                                    const std::shared_ptr<const NativeMaterialMotionPoses> &routes,
                                                    NativePreparationBudget *budget) {
    if (!UT_Thread::isMainThread())
      throw std::logic_error("Packed grouping requires the host thread");
    if (budget && capture != budget->capture) {
      clear();
      capture = budget->capture;
    }
    std::vector<std::uintptr_t> key{reinterpret_cast<std::uintptr_t>(rows.get())};
    if (routes) {
      const auto tokens = routes->route_identity(parent);
      key.insert(key.end(), tokens.begin(), tokens.end());
    }
    Work synchronous;
    auto &current = budget ? work[key] : synchronous;
    auto &result = *current.result;
    if (!result.rows) {
      result.rows = rows;
      current.routes = routes;
      result.row_groups.reserve(rows->values.size());
    }
    if (current.done)
      return current.result;
    auto route = parent;
    while (current.next < rows->values.size()) {
      const auto end = std::min(rows->values.size(), current.next + 256);
      while (current.next < end) {
        const auto index = current.next++;
        const auto &row = rows->values[index];
        result.procedural |= !row.stable_memory_source;
        if (row.unavailable) {
          if (!result.unavailable++)
            result.first_unavailable = index;
          result.row_groups.push_back(NativePackedGroups::missing);
          continue;
        }
        if (!row.prototype)
          throw std::logic_error("Packed group has neither an owned prototype nor a source diagnostic");
        result.index_ids += row.index_identity;
        result.ambiguous |= row.ambiguous_identity;
        if (row.invalid_velocity && !result.invalid_velocity++)
          result.first_velocity = index;
        std::vector<std::uintptr_t> identity;
        if (routes) {
          route.push_back({index, row.identity, row.index_identity});
          identity = routes->route_identity(route);
          route.pop_back();
        }
        auto stylesheet = descendant_styles(request, source, row.primitive_offset);
        // Members may share geometry and material but have different object settings.
        const int member = request.point_instancing && request.point_instancing->mode == NativeInstancingMode::Members
                               ? row.point_prototype
                               : -1;
        GroupKey group{row.prototype->get()->getUniqueId(),
                       request.material_mode == NativeMaterialMode::Native ? row.material_assignment
                                                                           : request.material_assignment(),
                       stylesheet, std::move(identity), member};
        const auto [found, inserted] = current.groups.try_emplace(std::move(group), result.groups.size());
        if (inserted)
          result.groups.push_back({index, 0, std::move(stylesheet)});
        ++result.groups[found->second].count;
        result.row_groups.push_back(found->second);
      }
      if (current.next < rows->values.size() && budget && budget->expired())
        throw NativePreparationPending{};
    }
    current.done = true;
    return current.result;
  }
};
struct NativeInstanceFieldShutter {
  struct Hierarchy {
    std::shared_ptr<const PreservedDetail> detail;
    std::shared_ptr<const NativePackedInstances> rows;
    std::map<std::int64_t, std::shared_ptr<const Hierarchy>> children;
    std::string warning;
  };
  NativeMotionWindow window;
  std::vector<std::shared_ptr<const Hierarchy>> samples;
};
struct NativeSceneAdapter::PreparedScene {
  std::map<std::pair<std::string, std::string>, SourceDiagnostic> diagnostics;
  const NativeSceneAdapter *owner{};
  std::uint64_t serial{};
  bool authoritative{}, consumed{};
  Resources desired;
  std::shared_ptr<NativeNavigationState::Origins> origins{std::make_shared<NativeNavigationState::Origins>()};
  std::vector<std::shared_ptr<const PreservedDetail>> roots;
  NativePackedSources packed_sources;
  struct Component {
    const GU_Detail *source{}; // Owned by roots or packed_sources.
    std::shared_ptr<const NativeGeometrySelection> selection;
    GeometryRequest request;
    std::string key;
    std::vector<NativeMaterialReference> references;
    NativeMotionWindow window;
    std::uint64_t multiplicity{1};
    std::string root, base_key;
  };
  std::vector<Component> components;
  struct ExternalMesh {
    std::shared_ptr<const MeshData> source, mesh;
    std::vector<NativeMaterialReference> references;
  };
  std::map<std::string, ExternalMesh> external_meshes;
  struct InstanceSource {
    const GU_Detail *source{}; // Read-only; retained by roots/packed_sources.
    GA_Offset primitive{GA_INVALID_OFFSET};
    std::string path;
    std::shared_ptr<const InstanceSource> parent;
    NativeMaterialRoute route;
    std::size_t population{};
  };
  std::map<std::string, std::shared_ptr<const InstanceSource>> instance_sources;
  // Recycled packed path: every attribute source of this capture in key
  // order (keys owned by the recycled object table); replaces instance_sources.
  std::vector<std::pair<const std::string *, std::shared_ptr<const InstanceSource>>> recycled_sources;
  bool recycled{};
  // A unique source hierarchy per sampled root/time. Packed resolution and
  // SOP cooking happen on the host; field tuple reads/correspondence are pure
  // work on these preserved owners during conversion.
  std::map<std::string, NativeInstanceFieldShutter> instance_field_shutters;
  // Geometry motion is assembled under its canonical prototype key. Layout
  // variants consume that same owned sample set, then attach declarations.
  std::map<std::string, std::string> instance_sample_sources;
  struct PackedSample {
    NativeDeformationSample input;
    std::string key;
    std::vector<std::int64_t> identifiers;
  };
  struct PackedDeformation {
    NativeGeometryKind kind;
    std::shared_ptr<const PackedSample> nominal;
    std::vector<std::shared_ptr<const PackedSample>> samples;
    std::shared_ptr<const NativeMotionWindow> window;
    std::string object, geometry_alias, warning;
    std::size_t component{};
  };
  std::map<std::string, PackedDeformation> packed_deformations;
  struct MotionAssembly {
    GeometryRequest request;
    NativeMotionWindow window;
    std::vector<std::string> objects;
    // Only transform/existence is consumed here. Shader/geometry samples
    // have their own owned recipes; do not copy full object bindings at
    // every shutter time for every instance.
    std::vector<std::map<std::string, Matrix>> sampled;
    std::vector<std::map<std::string, Matrix>> locals;
    std::set<std::string> population;
    std::vector<std::set<std::string>> sampled_populations;
    std::string cook_warning;
    bool retained{}, deformation{};
    std::shared_ptr<const NativeMaterialMotionPoses> material_poses;
    bool static_packed{};
  };
  std::vector<MotionAssembly> motion_assemblies;
  std::map<std::string, Matrix> nominal_locals;
  std::set<std::string> implicit_objects, ambiguous_objects;
  std::set<std::string> retained_deformation_keys;
  std::vector<std::string> retained_packed_roots;
  std::map<std::string, std::map<std::pair<std::string, std::string>, SourceDiagnostic>> root_diagnostics;
  // Host preparation resolves one rendering state per leaf prototype, while
  // pure per-instance object/alias expansion runs with the converter. No
  // GU_PrimPacked access or host callback survives in these recipes.
  struct PackedLeaves {
    struct Group {
      std::vector<ObjectData> objects;
      std::map<std::string, std::shared_ptr<const PackedSample>> samples;
      std::size_t first_component{}, last_component{};
      std::uint64_t count{};
      bool instance_attributes{};
    };
    struct Placement {
      std::size_t row{}, group{};
    };
    const GU_Detail *source{};
    std::shared_ptr<const InstanceSource> ancestors;
    std::string prefix, object_alias, geometry_alias;
    Matrix transform, local_transform;
    std::vector<Group> groups;
    std::shared_ptr<const NativePackedInstances> rows;
    std::shared_ptr<const NativePackedGroups> grouping;
    std::vector<std::size_t> group_indices;
    std::vector<Placement> placements;
    template <class F> void each_placement(F &&action) const {
      if (grouping) {
        for (std::size_t i = 0; i < grouping->row_groups.size(); ++i) {
          const auto group = grouping->row_groups[i];
          if (group != NativePackedGroups::missing && !collapsed_transform(rows->values[i].transform))
            action(rows->values[i], group_indices.at(group));
        }
      } else
        for (const auto &placement : placements)
          if (!collapsed_transform(rows->values.at(placement.row).transform))
            action(rows->values.at(placement.row), placement.group);
    }
    bool velocity{}, deformation{}, sampled{}, implicit{}, ambiguous{};
    int sample_step{-1};
    std::string source_path;
    NativeMotionWindow window;
    std::vector<Matrix> local_motion;
  };
  std::vector<PackedLeaves> packed_leaves;
  struct PackedPopulation {
    std::string root, prefix;
    int sample_step{-1};
    std::shared_ptr<const NativePackedInstances> rows;
  };
  std::vector<PackedPopulation> packed_populations;
  struct DirectInstances {
    std::shared_ptr<const NativePointInstanceSample> sample;
    std::vector<std::string> bindings;
    std::string root, geometry_alias;
    Matrix transform;
    ObjectData model;
  };
  std::vector<DirectInstances> direct_instances;
  bool packed_population_reuse{};
  std::map<std::string, std::pair<std::size_t, SourceDiagnostic>> invalid_packed_velocity;
  struct Volume {
    std::vector<std::int64_t> identifiers;
    std::shared_ptr<const PreparedVolume> data;
  };
  std::map<std::string, Volume> volumes;
  struct Deformation {
    std::shared_ptr<const PreparedDeformation> data;
    std::map<NativeGeometryKind, std::string> keys;
  };
  std::vector<Deformation> deformations;
  std::uint64_t packed_instances{}, index_ids{};
};
// What a converted generation records so that a later capture can recycle
// its tables (convert): the object table's identity and revision (every
// insert, erase or value write goes through NativeObjectTable::edit(), which
// bumps the revision), the origin map's identity (written only inside
// convert; its owner count is checked before reuse), the leaf aliases whose
// key sets are kept, and the table's nodes in key order. std::map nodes stay
// at their address until erased, and any erase bumps the revision first.
// Resources copies and prepare_commit removals drop the layout.
struct NativeSceneAdapter::PackedLayout {
  using ObjectEntry = std::pair<const std::string, ObjectData>;
  using OriginEntry = std::pair<const std::string, NativeNavigationState::Origin>;
  const NativeObjectTable::Table *table{};
  std::uint64_t revision{};
  const NativeNavigationState::Origins *origins{};
  std::vector<std::string> aliases;
  std::vector<const ObjectEntry *> ordered;
};
// The retired generation's recyclable containers, kept instead of freed.
struct NativeSceneAdapter::PackedSpare {
  std::shared_ptr<PackedLayout> layout;
  NativeObjectTable objects;
  std::shared_ptr<NativeNavigationState> navigation;
  std::map<std::string, std::set<std::string>> aliases;
};
std::shared_ptr<NativeSceneRoots> acquire_scene_roots(const std::vector<GeometryRequest> &requests, double time,
                                                      NativePointInstanceCache *instances) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene root acquisition requires the host thread");
  auto roots = std::make_shared<NativeSceneRoots>();
  if (instances &&
      std::any_of(requests.begin(), requests.end(), [](const auto &request) { return bool(request.point_instancing); }))
    instances->begin_capture();
  for (const auto &request : requests) {
    if (request.external_mesh)
      continue;
    auto &input = roots->inputs[request.source_key()];
    if (!input.detail && !input.diagnostic) {
      auto *sop = OPgetDirector()->findSOPNode(request.sop_path.c_str());
      if (!sop) {
        input.diagnostic =
            SourceDiagnostic{"geometry.missing_source", request.sop_path, request.geometry_key,
                             "The requested SOP no longer exists", "Omitting this source; rendering other objects"};
        continue;
      }
      const auto started = Clock::now();
      OP_Context context(time);
      NativeRootSource source(*sop, request, instances, time, nullptr,
                              [&](const SourceDiagnostic &issue) { input.warnings.push_back(issue); });
      const auto handle = source.getCookedGeoHandle(context);
      input.point_sample = source.point_sample();
      roots->cooking_seconds += elapsed(started);
      if (!handle.gdp() || source.error() >= UT_ERROR_ABORT) {
        input.diagnostic = SourceDiagnostic{"geometry.cook_failed", request.sop_path, request.geometry_key,
                                            "SOP cooking failed", "Omitting this source; rendering other objects"};
        continue;
      }
      input.detail = std::make_shared<PreservedDetail>(handle);
    }
    if (request.material_mode == NativeMaterialMode::Native)
      for (std::size_t i = 0; i < input.native_types.size(); ++i)
        input.native_types[i] = input.native_types[i] || request.render_types[i];
    input.packed = input.packed || request.render_types[static_cast<std::size_t>(NativeGeometryKind::Packed)];
  }
  return roots;
}
std::int64_t NativeSceneAdapter::canonical_prototype(const GU_Detail &detail) {
  const auto id = detail.getUniqueId();
  // A detail edited in place keeps its unique id: stamp its data ids too.
  std::vector<std::int64_t> stamp = {detail.getP() ? std::int64_t(detail.getP()->getDataId()) : -1,
                                     std::int64_t(detail.getTopology().getDataId()),
                                     std::int64_t(detail.getPrimitiveList().getDataId()),
                                     std::int64_t(detail.getMetaCacheCount()),
                                     std::int64_t(detail.getNumPoints()),
                                     std::int64_t(detail.getNumPrimitives())};
  auto &alias = prototype_aliases_[id];
  alias.epoch = prototype_epoch_;
  if (alias.canonical && alias.stamp == stamp) {
    if (const auto found = prototype_contents_.find(alias.content); alias.hashed && found != prototype_contents_.end())
      found->second.second = prototype_epoch_;
    return alias.canonical;
  }
  alias.stamp = std::move(stamp);
  alias.canonical = id;
  alias.hashed = false;
  if (partition_cache_.inspect(detail).find(NativeGeometryKind::Packed))
    return id; // Containers keep their identity; their leaves are canonical.
  const auto started = Clock::now();
  const auto content = prototype_content(detail);
  stats_.component_check_seconds += elapsed(started);
  if (!content)
    return id;
  auto [found, inserted] = prototype_contents_.try_emplace(*content, id, prototype_epoch_);
  found->second.second = prototype_epoch_;
  alias.canonical = found->second.first;
  alias.content = *content;
  alias.hashed = true;
  return alias.canonical;
}
void NativeSceneAdapter::prewarm_roots(NativeSceneRoots &roots) {
  release_retired();
  const auto scan = [&](NativeSceneRoots::Input &input) {
    if (input.scanned)
      return;
    if (input.diagnostic) {
      input.scanned = input.expanded = true;
      return;
    }
    const auto &source = *input.detail->get();
    const auto &partitions = partition_cache_.inspect(source);
    for (const auto &selection : partitions.selections)
      if (selection.active) {
        if (input.native_types.at(static_cast<std::size_t>(selection.kind)))
          binding_cache_.inspect(source, &selection);
        if (selection.kind == NativeGeometryKind::Points || selection.kind == NativeGeometryKind::Curves)
          size_cache_.uses_fallback(source, selection);
      }
    input.scanned = true;
  };
  for (auto &[path, input] : roots.inputs)
    scan(input);
  for (auto &[id, input] : roots.prototypes)
    scan(input);
}
bool NativeSceneAdapter::acquire_packed_roots(NativeSceneRoots &roots) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed acquisition requires the host thread");
  const auto deadline = Clock::now() + std::chrono::milliseconds(20);
  std::vector<NativeSceneRoots::Input *> frontier;
  const auto collect = [&](auto &inputs) {
    for (auto &[key, input] : inputs)
      if (input.scanned && !input.expanded)
        frontier.push_back(&input);
  };
  collect(roots.inputs);
  collect(roots.prototypes);
  bool more = false;
  for (auto *input : frontier) {
    if (!input->packed) {
      input->expanded = true;
      if (Clock::now() >= deadline)
        return true;
      continue;
    }
    const auto &source = *input->detail->get();
    const auto *selection = partition_cache_.inspect(source).find(NativeGeometryKind::Packed);
    if (!selection) {
      input->expanded = true;
      if (Clock::now() >= deadline)
        return true;
      continue;
    }
    const auto count =
        std::size_t(selection->full_detail ? source.getNumPrimitives() : selection->primitives.entries());
    while (input->next_packed < count) {
      const auto end = std::min(count, input->next_packed + 256);
      while (input->next_packed < end) {
        const auto index = input->next_packed++;
        const auto offset =
            selection->full_detail ? source.primitiveOffset(GA_Index(index)) : selection->primitives(exint(index));
        const auto *primitive = dynamic_cast<const GU_PrimPacked *>(source.getPrimitive(offset));
        if (!primitive)
          throw std::logic_error("Invalid frozen packed selection");
        // Viewport boxes are presentation only. Blender evaluation belongs to
        // the capture's external resource plan, never GU packed callbacks.
        if (packed_blender_reference(*primitive)) {
          roots.packed_details.insert_or_assign({source.getUniqueId(), offset}, nullptr);
          continue;
        }
        const auto handle = packed_contents(source, *selection, index, *primitive); // F3: fragments
        if (!handle) {
          roots.packed_details.insert_or_assign({source.getUniqueId(), offset}, nullptr);
          continue;
        }
        auto &child = roots.prototypes[handle.gdp()->getUniqueId()];
        if (!child.detail) {
          if (input->depth >= 64)
            throw std::invalid_argument("Excessive packed nesting");
          child.detail = std::make_shared<PreservedDetail>(handle);
          child.depth = input->depth + 1;
          child.packed = true;
          more = true;
        }
        for (std::size_t i = 0; i < child.native_types.size(); ++i)
          if (input->native_types[i] && !child.native_types[i]) {
            child.native_types[i] = true;
            child.scanned = child.expanded = false;
            child.next_packed = 0;
            more = true;
          }
        roots.packed_details.insert_or_assign({source.getUniqueId(), offset}, child.detail);
      }
      if (Clock::now() >= deadline)
        return true;
    }
    input->expanded = true;
  }
  return more;
}
std::shared_ptr<NativeSceneAdapter::PreparedScene>
NativeSceneAdapter::prepare(const std::vector<GeometryRequest> &requested, double time, bool authoritative,
                            const NativeMotionSettings &motion_settings,
                            const NativeMaterialPreparation &prepare_materials,
                            const std::shared_ptr<const NativeSceneRoots> &roots, NativePreparationBudget *budget) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Native scene preparation requires the host thread");
  auto prepared = std::make_shared<PreparedScene>();
  prepared->owner = this;
  prepared->serial = ++preparation_serial_;
  prepared->authoritative = authoritative;
  const auto shutter = native_motion_window(time, motion_settings);
  auto window = shutter;
  if (!budget || !budget->turns) {
    stats_.cooking_seconds = stats_.conversion_seconds = 0;
    stats_.packed_assembly_seconds.clear();
    stats_.packed_template_components = stats_.packed_expanded_components = stats_.packed_final_components = 0;
    stats_.packed_deformation_tasks = stats_.packed_profile_groups = 0;
    if (roots)
      stats_.cooking_seconds = roots->cooking_seconds;
    stats_.motion_cooks = 0;
    stats_.motion_samples = window.enabled ? window.times.size() : 0;
    stats_.material_motion_cooks = stats_.material_motion_samples = stats_.material_motion_cache_hits = 0;
    stats_.direct_instances_expanded = stats_.direct_population_reuses = 0;
    stats_.packed_population_reuses = 0;
    stats_.adapter_full_object_updates = stats_.adapter_pose_updates = stats_.adapter_attribute_updates =
        stats_.adapter_pose_attribute_updates = 0;
    stats_.packed_recycled_objects = 0;
    stats_.packed_recycle_fallback.clear();
    // Keep prototype content identities seen in this or the previous capture.
    ++prototype_epoch_;
    std::erase_if(prototype_aliases_, [&](const auto &entry) { return entry.second.epoch + 2 < prototype_epoch_; });
    std::erase_if(prototype_contents_, [&](const auto &entry) { return entry.second.second + 2 < prototype_epoch_; });
  }
  stats_.shutter_open_seconds = window.times.front();
  stats_.shutter_close_seconds = window.times.back();
  pending_.reset();
  pending_population_reuse_ = false;
  if (!authoritative)
    return prepared;
  if (!material_motion_cache_)
    material_motion_cache_ = std::make_shared<NativeMaterialMotionCache>();
  if (!packed_preparation_cache_)
    packed_preparation_cache_ = std::make_shared<NativePackedPreparationCache>();
  if (!packed_grouping_cache_)
    packed_grouping_cache_ = std::make_shared<NativePackedGroupingCache>();
  const auto packed_ledger = roots ? std::shared_ptr<const NativePackedDetails>(roots, &roots->packed_details)
                                   : std::shared_ptr<const NativePackedDetails>{};
  {
    std::set<std::string> roots;
    for (const auto &request : requested)
      roots.insert(request.object_key);
    material_motion_cache_->retire(roots);
  }
  material_paths_.clear();
  stylesheet_resources_.clear();
  auto &desired = prepared->desired;
  desired.navigation = std::make_shared<NativeNavigationState>();
  desired.navigation->time = time;
  desired.navigation->motion = motion_settings;
  desired.navigation->origins = prepared->origins;
  std::string diagnostic_root;
  const auto record_issue = [&](SourceDiagnostic issue) {
    if (!diagnostic_root.empty())
      retain_diagnostic(prepared->root_diagnostics[diagnostic_root], issue);
    retain_diagnostic(prepared->diagnostics, issue);
  };
  // Deformation per SOP output, decided from the frame's own top-level
  // attributes before anything reads a mode, so nothing is cooked at another
  // time for it: point v; point w where the output holds packed primitives or
  // is an Instance OBJ's template points; a vel/velocity volume field.
  // Velocity if Available becomes Velocity when one is there and Geometry
  // Samples (Deformation) otherwise. Packed contents and point prototypes copy
  // the outer request, so they follow its decision. Velocity or Acceleration
  // chosen on the object itself (not From ROP) without any of them reports
  // one warning naming the object; Acceleration without accel, a note.
  std::vector<GeometryRequest> resolved_requests;
  const bool resolve_motion = std::any_of(requested.begin(), requested.end(), [&](const auto &request) {
    return request.motion == NativeGeometryMotion::VelocityIfAvailable ||
           (shutter.enabled && request.motion_blur && request.motion_explicit && attribute_motion(request.motion));
  });
  if (resolve_motion) {
    resolved_requests = requested;
    for (auto &request : resolved_requests) {
      const bool available = request.motion == NativeGeometryMotion::VelocityIfAvailable;
      if (!available &&
          !(shutter.enabled && request.motion_blur && request.motion_explicit && attribute_motion(request.motion)))
        continue;
      const GU_Detail *detail = nullptr;
      GU_ConstDetailHandle handle;
      if (request.external_mesh || request.sop_path.empty()) {
      } else if (roots) {
        const auto found = roots->inputs.find(request.source_key());
        if (found != roots->inputs.end() && found->second.detail)
          detail = found->second.detail->get();
      } else if (auto *sop = OPgetDirector()->findSOPNode(request.sop_path.c_str())) {
        // The frame cook the capture below needs anyway; later reads hit Houdini's cache.
        const auto started = Clock::now();
        OP_Context context(time);
        handle = sop->getCookedGeoHandle(context);
        stats_.cooking_seconds += elapsed(started);
        detail = handle.gdp();
      }
      bool carries_v = false, carries_w = false, carries_field = false;
      if (detail) {
        carries_v = detail->findPointAttribute("v") != nullptr;
        const auto &partitions = partition_cache_.inspect(*detail);
        carries_w = (request.point_instancing || partitions.find(NativeGeometryKind::Packed)) &&
                    detail->findPointAttribute("w") != nullptr;
        if (const auto *volumes = partitions.find(NativeGeometryKind::Volume)) {
          GA_ROHandleS names(detail->findPrimitiveAttribute("name"));
          if (names.isValid())
            for (GA_Iterator it(volumes->primitive_range(*detail)); !carries_field && !it.atEnd(); ++it) {
              const UT_StringHolder name = names.get(*it);
              carries_field = name == "vel" || name == "velocity" || name == "vel.x" || name == "velocity.x";
            }
        }
      }
      const bool carries = carries_v || carries_w || carries_field;
      if (available) {
        // An unavailable source keeps Geometry Samples; its own diagnostic follows.
        request.motion = carries ? NativeGeometryMotion::Velocity : NativeGeometryMotion::Deformation;
        continue;
      }
      if (!detail)
        continue;
      diagnostic_root = request.object_key;
      const auto &object = request.object_path.empty() ? request.sop_path : request.object_path;
      const bool acceleration = request.motion == NativeGeometryMotion::Acceleration;
      const char *mode = acceleration ? "Acceleration" : "Velocity";
      // An object with no points yet (particles born later) has nothing to blur: no warning.
      if (!carries && detail->getNumPoints() > 0)
        record_issue({"motion.missing_velocity", object, request.geometry_key,
                      std::string(mode) + " deformation blur needs point v (or w on packed and instance points), but " +
                          request.sop_path + " has none",
                      "Rendering it without deformation blur; camera and object transform blur still apply. Add v "
                      "(Trail SOP) or set Deformation to Geometry Samples",
                      "warning", "v"});
      else if (acceleration && carries_v && !detail->findPointAttribute("accel"))
        record_issue({"motion.missing_acceleration", object, request.geometry_key,
                      request.sop_path + " has no point accel; Acceleration uses v alone (straight paths)",
                      "Rendering with Velocity deformation blur", "info", "accel"});
      diagnostic_root.clear();
    }
  }
  const auto &requests = resolve_motion ? resolved_requests : requested;
  auto &packed_instances = prepared->packed_instances;
  auto &index_ids = prepared->index_ids;
  std::set<std::string> captured;
  std::map<std::string, Matrix> *poses = nullptr;
  std::map<std::string, Matrix> *pose_locals = nullptr;
  std::vector<std::string> *captured_objects = nullptr;
  std::vector<ObjectData> *object_templates = nullptr;
  std::map<std::string, std::shared_ptr<const PreparedScene::PackedSample>> *deformation_templates = nullptr;
  bool defer_deformation_leaves = false;
  std::set<std::string> deferred_deformation_roots;
  std::string root_object_alias, root_geometry_alias;
  std::map<std::int64_t, bool> leaf_prototypes;
  NativeRootSource *capture_sop = nullptr;
  std::shared_ptr<NativeMaterialMotionContext> material_context;
  std::vector<NativeMaterialRoute> binding_route;
  struct BindingRouteGuard {
    std::vector<NativeMaterialRoute> &route;
    BindingRouteGuard(std::vector<NativeMaterialRoute> &route, NativeMaterialRoute next) : route(route) {
      route.push_back(std::move(next));
    }
    ~BindingRouteGuard() { route.pop_back(); }
  };
  auto &nominal_local_transforms = prepared->nominal_locals;
  std::map<std::string, std::vector<Matrix>> packed_velocity_locals;
  auto &implicit_objects = prepared->implicit_objects;
  auto &ambiguous_objects = prepared->ambiguous_objects;
  std::set<std::string> procedural_packed_roots;
  std::map<std::string, std::set<std::string>> packed_populations;
  std::set<std::string> *sampled_packed_population = nullptr;
  using PackedSample = PreparedScene::PackedSample;
  using PackedDeformation = PreparedScene::PackedDeformation;
  std::shared_ptr<const NativeMotionWindow> packed_window;
  std::map<const PackedSample *, std::tuple<std::shared_ptr<PackedSample>, NativeRootSource *, NativeGeometryKind,
                                            std::shared_ptr<const NativeMotionWindow>>>
      nominal_samples;
  auto &packed_deformations = prepared->packed_deformations;
  auto &retained_deformation_keys = prepared->retained_deformation_keys;
  using RetainedPrototypeKey = std::tuple<std::string, std::string, std::set<std::string>, bool>;
  std::map<RetainedPrototypeKey, std::string> retained_prototypes;
  std::size_t packed_sample_index{};
  std::map<std::string, std::vector<NativeSceneRoots::ShutterInput>> packed_shutter_roots;
  auto &packed_sources = prepared->packed_sources;
  std::optional<PackedPopulationState> packed_population;
  bool capture_packed_bindings{};
  std::set<std::tuple<const GeometryRequest *, NativeMaterialAssignment, AttributeLayout>> packed_binding_states;
  std::map<std::string, std::pair<std::size_t, SourceDiagnostic>> unavailable_packed;
  auto &invalid_packed_velocity = prepared->invalid_packed_velocity;
  const auto velocity_issue = [&](SourceDiagnostic issue) {
    issue.attribute = "v";
    auto [found, inserted] = invalid_packed_velocity.try_emplace(issue.source, 0, std::move(issue));
    ++found->second.first;
  };
  // First Houdini placement of each Blender object/instance under a Houdini root (apply_blender_object).
  std::set<std::pair<std::string, std::string>> blender_placements;
  const auto blender_first = [&](const std::string &root, const GeometryRequest &member) {
    return member.blender_object && blender_placements.emplace(root, member.object_key).second;
  };
  const auto object = [&](ObjectData data) {
    if (object_templates) {
      object_templates->push_back(std::move(data));
      return;
    }
    if (poses) {
      if (!poses->emplace(data.key, data.transform).second)
        throw std::invalid_argument("Duplicate packed shutter identity");
      return;
    }
    const auto [stored, inserted] = desired.objects.try_emplace(data.key, std::move(data));
    if (!inserted)
      throw std::invalid_argument("Duplicate native object identity: " + data.key);
    if (captured_objects)
      captured_objects->push_back(stored->first);
  };
  const auto prepare_volume_input = [&](const GU_Detail &source, const GeometryRequest &request, const std::string &key,
                                        const NativeGeometrySelection &selection) {
    auto data = prepare_volume(source, request, selection, key, 0, window);
    auto fingerprint = volume_capture_identifiers(*data);
    prepared->volumes.emplace(key, PreparedScene::Volume{std::move(fingerprint), std::move(data)});
  };
  std::set<std::int64_t> ancestors;
  std::map<const GU_Detail *, std::shared_ptr<const NativeGeometryPartitions>> frozen_partitions;
  struct ComponentPlan {
    std::shared_ptr<const NativeGeometrySelection> selection;
    GeometryRequest request;
    std::string key;
    std::vector<NativeMaterialReference> references;
    std::shared_ptr<PackedSample> sample;
  };
  // This transaction-local plan is scoped to a root and shutter visit. Only
  // distinct prototype/binding states resolve geometry/material semantics.
  // Per-instance object properties and identity remain outside the plan.
  using PlanKey =
      std::tuple<std::int64_t, std::string, NativeMaterialAssignment, std::string, NativePointRenderMode, bool>;
  std::map<PlanKey, std::vector<ComponentPlan>> component_plans;
  const auto *host_profile_value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY");
  const bool host_profiling = host_profile_value && std::strcmp(host_profile_value, "1") == 0;
  const auto host_phase = [&](const char *name, auto &&action) -> decltype(auto) {
    if (!host_profiling)
      return action();
    const auto began = Clock::now();
    try {
      decltype(auto) value = action();
      stats_.packed_assembly_seconds[name] += elapsed(began);
      return value;
    } catch (...) {
      stats_.packed_assembly_seconds[name] += elapsed(began);
      throw;
    }
  };
  std::shared_ptr<const PreparedScene::InstanceSource> instance_source;
  std::function<std::string(const GeometryRequest &, const GeometryRequest &, const NativeMaterialAssignment &,
                            const std::vector<UniformAttribute> &)>
      external_geometry;
  struct ExternalMotionRecord {
    std::shared_ptr<const GeometryRequest> nominal;
    std::vector<std::shared_ptr<const GeometryRequest>> samples;
    const GeometryRequest *owner{};
    NativeMaterialAssignment binding;
    std::vector<UniformAttribute> fields;
    NativeMotionWindow window;
    std::string reference;
  };
  std::map<std::string, ExternalMotionRecord> external_motion;
  const GeometryRequest *external_motion_owner{};
  using BlenderMembers = std::map<std::pair<std::string, double>, const NativePointPrototype *>;
  std::map<const NativePointInstancing *, BlenderMembers> blender_members;
  std::function<void(const GU_Detail &, const GeometryRequest &, const std::string &, const std::string &,
                     const Matrix &, int, const Matrix &, bool, bool, const std::vector<Matrix> *, double)>
      visit;
  visit = [&](const GU_Detail &source, const GeometryRequest &input_request, const std::string &geometry_key,
              const std::string &object_key, const Matrix &transform, int depth, const Matrix &local_transform,
              bool implicit, bool ambiguous, const std::vector<Matrix> *velocity_locals, double style_time) {
    if (depth > 64 || !ancestors.insert(source.getUniqueId()).second)
      throw std::invalid_argument("Cyclic or excessive packed nesting in " + input_request.sop_path);
    // One immutable selection set per retained source. Instances and
    // material variants share its potentially large offset lists. The
    // original cache entry may be replaced by a later shutter cook.
    // Pose-only shutter roots are retained only for that sample. Their
    // addresses may be reused by a later cook, so never memoize them by
    // address across samples; the data-ID cache validates each visit.
    auto partition_owner = poses ? partition_cache_.acquire(source) : frozen_partitions[&source];
    if (!partition_owner) {
      partition_owner = partition_cache_.acquire(source);
      frozen_partitions[&source] = partition_owner;
    }
    const auto &partitions = *partition_owner;
    const bool packed_root = input_request.render_types.at(static_cast<std::size_t>(NativeGeometryKind::Packed)) &&
                             partitions.find(NativeGeometryKind::Packed);
    if (partitions.unsupported_count) {
      std::string examples;
      for (const auto index : partitions.unsupported_examples)
        examples += (examples.empty() ? "" : ", ") + std::to_string(index);
      record_issue(
          {"geometry.unsupported_primitive", input_request.sop_path, geometry_key,
           std::to_string(partitions.unsupported_count) + " unsupported primitives (examples: " + examples + ")",
           "Omitting unsupported primitives; rendering supported components"});
    }
    if (!poses)
      desired.partition_details.insert(source.getUniqueId());
    auto styled_input = input_request;
    if (styled_input.material_mode == NativeMaterialMode::Native) {
      styled_input.native_styles = host_phase("host_style_evaluation", [&]() {
        return style_cache_.prepare(source, styled_input.stylesheet, styled_input.stylesheet_resources, style_time,
                                    styled_input.object_path.empty() ? styled_input.sop_path : styled_input.object_path,
                                    host_profiling ? &stats_.packed_assembly_seconds : nullptr, budget, &partitions,
                                    &styled_input.render_types);
      });
      if (styled_input.native_styles)
        stylesheet_resources_.insert(styled_input.native_styles->resources.begin(),
                                     styled_input.native_styles->resources.end());
      for (const auto &message : styled_input.stylesheet_errors)
        record_issue({"material.stylesheet_ignored", styled_input.sop_path, geometry_key, message,
                      "Keeping supported stylesheet rules and underlying bindings", "warning", "material_stylesheet"});
      if (styled_input.native_styles)
        for (auto issue : styled_input.native_styles->diagnostics) {
          issue.source = styled_input.sop_path;
          issue.resource = geometry_key;
          record_issue(std::move(issue));
        }
    }
    const auto binding_profile =
        material_context && !poses
            ? host_phase("host_motion_profile", [&]() { return material_context->profile(binding_route); })
            : nullptr;
    const PlanKey plan_key{source.getUniqueId(),
                           geometry_key,
                           input_request.material_assignment(),
                           (binding_profile ? binding_profile->signature : std::string{}) + styled_input.stylesheet,
                           input_request.point_render_mode,
                           input_request.point_splat_default_material};
    auto [plan, new_plan] = component_plans.try_emplace(plan_key);
    auto &components = plan->second;
    if (new_plan)
      for (const auto &selection : partitions.selections) {
        if (!selection.active)
          continue;
        if (!input_request.render_types.at(static_cast<std::size_t>(selection.kind)))
          continue;
        auto request = styled_input;
        const bool packed = selection.kind == NativeGeometryKind::Packed;
        const bool vdb = selection.kind == NativeGeometryKind::Volume;
        const bool point_source = selection.kind == NativeGeometryKind::Points;
        const bool curve_source = selection.kind == NativeGeometryKind::Curves;
        if (window.enabled && request.motion == NativeGeometryMotion::Deformation && vdb) {
          request.motion = NativeGeometryMotion::TransformOnly;
          record_issue({"motion.unsupported_method", request.sop_path, geometry_key,
                        "Sparse volume deformation sampling is not supported",
                        "Using requested-frame geometry; retaining independently supported transform motion"});
        }
        std::string binding_key;
        std::vector<NativeMaterialReference> references;
        if (request.material_mode == NativeMaterialMode::Native) {
          desired.binding_details.insert(source.getUniqueId());
          const auto &state = host_phase("host_binding_inspect",
                                         [&]() -> const auto & { return binding_cache_.inspect(source, &selection); });
          request.native_material_policy = state.policy;
          for (auto issue : state.diagnostics) {
            if (request.native_styles && issue.code == "material.unsupported_override" && issue.attribute.empty())
              continue;
            issue.source = request.sop_path + "/" + issue.source;
            issue.resource = geometry_key;
            record_issue(std::move(issue));
          }
          if (state.policy && (!request.object_material_path.empty() || !state.detail_path.empty() ||
                               !state.primitive_paths.empty() || (point_source && !state.point_paths.empty())))
            for (auto issue : state.policy->diagnostics) {
              issue.source = request.sop_path;
              issue.resource = geometry_key;
              issue.message += " [" + std::string(geometry_kind_name(selection.kind)) + "]";
              record_issue(std::move(issue));
            }
          const auto ignored = [&](const std::string &reason) {
            record_issue({"material.unsupported_binding", request.sop_path, geometry_key, reason,
                          "Using supported object/detail/primitive bindings"});
          };
          if (!packed) {
            if (point_source && state.vertex_bindings)
              ignored("Vertex material assignments/overrides require a per-key shader adapter: " + request.sop_path);
            if (curve_source && state.vertex_stylesheets)
              ignored("Per-key vertex material style sheets on hair require an explicit adapter: " + request.sop_path);
            if (vdb && (!state.point_paths.empty() || state.point_overrides || state.vertex_bindings))
              ignored("VDB fields support object/detail/primitive materials, not point or vertex bindings/overrides: " +
                      request.sop_path);
            std::map<std::string, NativeMaterialReference> unique;
            auto frozen = host_phase("host_material_lookup", [&]() {
              return freeze_native_materials(state, request, point_source, vdb, poses != nullptr);
            });
            frozen = host_phase("host_style_lookup", [&]() {
              return freeze_styled_materials(source, selection, request, std::move(frozen), vdb, &style_binding_cache_,
                                             budget);
            });
            for (auto issue : frozen->diagnostics) {
              issue.source = request.sop_path;
              issue.resource = geometry_key;
              record_issue(std::move(issue));
            }
            if (selection.kind == NativeGeometryKind::Mesh && binding_profile) {
              frozen = binding_profile->materials;
              for (auto issue : binding_profile->diagnostics) {
                issue.resource = geometry_key;
                record_issue(std::move(issue));
              }
            }
            request.native_fallback.reset();
            const bool unassigned = frozen->unassigned.value_or(state.fallback_used && state.detail_path.empty() &&
                                                                request.object_material_path.empty());
            if (!unassigned)
              request.material_key.clear();
            if (unassigned && request.material_key.empty())
              throw std::invalid_argument("Unassigned native primitives require a nonempty fallback material key");
            for (const auto &[assignment, reference] : *frozen) {
              material_paths_.insert(reference.path);
              const auto [found, inserted] = unique.emplace(reference.key, reference);
              if (!inserted && found->second != reference)
                throw std::logic_error("Native material variant identity collision");
            }
            if (frozen->elements)
              for (const auto &reference : frozen->elements->references)
                unique.emplace(reference->key, *reference);
            request.native_materials = std::move(frozen);
            if (prepare_materials &&
                (!poses || (window.enabled && request.motion == NativeGeometryMotion::Deformation))) {
              const auto names =
                  host_phase("host_material_graphs", [&]() { return prepare_materials(*request.native_materials); });
              request.shader_attributes.insert(names.begin(), names.end());
            }
            // Fully assigned prototypes do not depend on outer fallbacks.
            std::set<std::string> keys;
            for (auto &[key, reference] : unique) {
              keys.insert(key);
              references.push_back(std::move(reference));
            }
            if (unassigned)
              keys.insert(request.material_key);
            if (selection.kind == NativeGeometryKind::Mesh && prepare_materials.has_pointiness) {
              request.pointiness = false;
              for (const auto &key : keys)
                request.pointiness |= prepare_materials.has_pointiness(key, false);
            }
            if (vdb) {
              if (keys.empty())
                throw std::logic_error("Sparse volume has no captured material binding");
              // Retain every owned binding until background field
              // validation selects the actual usable density. Keep
              // the configured fallback for unassigned primitives.
              if (request.material_key.empty())
                request.material_key = *keys.begin();
            }
            binding_key = "/native-bindings";
            if (request.native_styles)
              binding_key += "/styles:" + request.native_styles->signature;
            for (const auto &key : keys)
              binding_key += ":" + std::to_string(key.size()) + ":" + key;
            // An OBJ fallback can change which existing shader slot unassigned
            // elements use without changing the shader set or any GU data id.
            // Resolve its stable identity once; renaming the same material does
            // not split geometry. Fully assigned/detail-bound sources omit it.
            if (state.fallback_used && state.detail_path.empty() && !request.object_material_path.empty()) {
              const auto fallback = resolve_native_material(request.material_assignment(), vdb).key;
              binding_key += "/object-fallback:" + std::to_string(fallback.size()) + ":" + fallback;
            }
            if (selection.kind == NativeGeometryKind::Mesh && binding_profile && !binding_profile->signature.empty())
              binding_key += "/binding-motion:" + binding_profile->signature;
          }
        }
        const auto suffix = std::string("/part:") + geometry_kind_name(selection.kind);
        const auto render_key =
            geometry_key + suffix + binding_key +
            (window.enabled && request.motion == NativeGeometryMotion::Acceleration  ? "/motion:acceleration"
             : window.enabled && attribute_motion(request.motion)                    ? "/motion:velocity"
             : window.enabled && request.motion == NativeGeometryMotion::Deformation ? "/motion:deformation"
                                                                                     : "") +
            (selection.kind == NativeGeometryKind::Mesh && !request.motion_blur ? "/motion:disabled" : "");
        std::string key;
        if (vdb)
          key = "volume:" + render_key +
                "/step:" + std::to_string(std::bit_cast<std::uint32_t>(request.volume_step_size)) +
                "/object-space:" + std::to_string(request.volume_object_space);
        else if (point_source || curve_source) {
          // Deformation may remove a size attribute at another shutter time.
          // Until all-time default use is classified, distinguish defaults
          // conservatively rather than sharing incompatible motion radii.
          const bool fallback = (window.enabled && request.motion == NativeGeometryMotion::Deformation) ||
                                size_cache_.uses_fallback(source, selection);
          key = (curve_source ? "curves:" : "points:") + render_key +
                (fallback ? "/fallback:" + std::to_string(std::bit_cast<std::uint32_t>(
                                               curve_source ? request.curve_width : request.point_radius))
                          : "") +
                (curve_source ? "/shape:" + std::to_string(static_cast<int>(request.curve_shape))
                              : "/point-mode:" + std::to_string(static_cast<int>(request.point_render_mode)) +
                                    "/splat-default:" + std::to_string(request.point_splat_default_material)) +
                (selection.parametric_curves ? "/segments:" + std::to_string(request.curve_segments) : "");
        } else if (!packed)
          key = render_key + subdivision_signature(request.subdivision);
        std::shared_ptr<PackedSample> sample;
        if ((depth || packed_root) && window.enabled && request.motion == NativeGeometryMotion::Deformation && !packed)
          sample = std::make_shared<PackedSample>(
              PackedSample{{packed_sources.preserved(source.getUniqueId()), request,
                            std::shared_ptr<const NativeGeometrySelection>(partition_owner, &selection)},
                           key});
        components.push_back({std::shared_ptr<const NativeGeometrySelection>(partition_owner, &selection),
                              std::move(request), std::move(key), std::move(references), std::move(sample)});
      }
    if (window.enabled && input_request.motion == NativeGeometryMotion::Deformation && !poses && depth == 0 &&
        !packed_root) {
      std::map<NativeGeometryKind, GeometryRequest> sampling_requests;
      bool dirty = false;
      for (const auto &component : components) {
        if (component.request.motion != NativeGeometryMotion::Deformation ||
            component.selection->kind == NativeGeometryKind::Packed)
          continue;
        sampling_requests.emplace(component.selection->kind, component.request);
        if (captured.contains(component.key))
          continue;
        const auto found = cache_.find(component.key);
        auto fingerprint_request = component.request;
        if (found != cache_.end())
          fingerprint_request.shader_attributes.insert(found->second.shader_attributes.begin(),
                                                       found->second.shader_attributes.end());
        const auto fingerprint =
            deformation_identifiers(source, *capture_sop, fingerprint_request, window, component.selection->kind);
        const bool needs_capture = fingerprint[0] == GA_INVALID_DATAID || fingerprint[1] == GA_INVALID_DATAID ||
                                   fingerprint[2] == GA_INVALID_DATAID || found == cache_.end() ||
                                   found->second.identifiers != fingerprint ||
                                   found->second.material_key != component.request.material_key;
        if (prepare_materials && !needs_capture) {
          auto required = component.request.shader_attributes;
          for (const auto &reference : found->second.material_references) {
            const auto current = refresh_native_material_reference(reference);
            dirty |= !same_native_material_resource(current, reference);
            NativeMaterialLookup retained;
            retained.emplace(current.assignment(), current);
            const auto names = prepare_materials(retained);
            required.insert(names.begin(), names.end());
          }
          dirty |= required != found->second.shader_attributes;
        }
        dirty |= needs_capture;
      }
      if (dirty) {
        PreparedScene::Deformation sampled;
        sampled.data =
            prepare_deformation_set(capture_sop->node(), sampling_requests, window, stats_, prepare_materials);
        for (const auto &component : components)
          if (component.request.motion == NativeGeometryMotion::Deformation &&
              component.selection->kind != NativeGeometryKind::Packed && !captured.contains(component.key))
            sampled.keys.emplace(component.selection->kind, component.key);
        prepared->deformations.push_back(std::move(sampled));
      }
    }
    for (const auto &component : components) {
      const auto &selection = *component.selection;
      const auto &request = component.request;
      const auto &key = component.key;
      const auto component_object = object_key + "/part:" + geometry_kind_name(selection.kind);
      const bool packed = selection.kind == NativeGeometryKind::Packed;
      if (!packed) {
        if ((depth || packed_root) && window.enabled && request.motion == NativeGeometryMotion::Deformation) {
          const auto &sample = component.sample;
          if (!sample)
            throw std::logic_error("Missing shared packed deformation sample");
          if (object_templates) {
            if (!deformation_templates)
              throw std::logic_error("Missing packed deformation template ledger");
            deformation_templates->emplace(component_object, sample);
            if (!poses)
              nominal_samples.try_emplace(sample.get(), sample, capture_sop, selection.kind, packed_window);
          } else if (!poses) {
            PackedDeformation task;
            task.kind = selection.kind;
            task.nominal = sample;
            task.window = packed_window;
            task.samples.resize(window.times.size());
            task.object = component_object;
            task.geometry_alias = root_geometry_alias;
            task.component = prepared->components.size();
            nominal_samples.try_emplace(sample.get(), sample, capture_sop, selection.kind, packed_window);
            if (!packed_deformations.emplace(component_object, std::move(task)).second)
              throw std::logic_error("Duplicate packed deformation recipe");
          } else if (auto found = packed_deformations.find(component_object); found != packed_deformations.end()) {
            found->second.samples.at(packed_sample_index) = sample;
          }
        }
        if (!poses) {
          desired.geometry.insert(key);
          desired.geometry_details[key].insert(ancestors.begin(), ancestors.end());
          prepared->components.push_back(
              {&source, component.selection, request, key, component.references, window, 1, root_object_alias, key});
          captured.insert(key);
          if (selection.kind == NativeGeometryKind::Volume && !prepared->volumes.contains(key))
            prepare_volume_input(source, request, key, selection);
        }
        ObjectData instance{component_object, key, transform, request.visibility,
                            depth ? random_id(component_object) : request.random_id};
        instance.properties = input_request.properties;
        if (poses) {
          if (pose_locals && !object_templates)
            pose_locals->emplace(component_object, local_transform);
        } else if (!object_templates)
          prepared->origins->emplace(component_object,
                                     NativeNavigationState::Origin{root_object_alias, local_transform});
        if (window.enabled) {
          if (!poses && !object_templates)
            nominal_local_transforms[component_object] = local_transform;
          if (!poses && !object_templates && velocity_locals)
            packed_velocity_locals.emplace(component_object, *velocity_locals);
          if (implicit)
            implicit_objects.insert(component_object);
          if (ambiguous)
            ambiguous_objects.insert(component_object);
        }
        if (!poses && !object_templates && instance_source && selection.kind != NativeGeometryKind::Volume &&
            std::any_of(request.shader_attributes.begin(), request.shader_attributes.end(),
                        [&](const auto &name) { return !named_attribute(source, name, true); }))
          prepared->instance_sources.emplace(component_object, instance_source);
        object(std::move(instance));
        if (depth && !poses && !object_templates)
          ++packed_instances;
        continue;
      }
      const auto captured_instances = host_phase("host_packed_capture", [&]() {
        return packed_preparation_cache_->prepare(source, request, selection, packed_sources,
                                                  material_context && !poses ? material_context->packed_details()
                                                  : !poses                   ? packed_ledger
                                                           : std::shared_ptr<const NativePackedDetails>{},
                                                  window.enabled && attribute_motion(request.motion), budget, false);
      });
      const auto &instances = captured_instances->values;
      if (capture_packed_bindings && packed_population &&
          (depth != 0 || std::any_of(instances.begin(), instances.end(),
                                     [](const auto &row) { return row.blender_reference.empty(); })))
        packed_population.reset();
      PreparedScene::PackedLeaves leaves;
      leaves.rows = captured_instances;
      leaves.source = &source;
      leaves.ancestors = instance_source;
      leaves.prefix = component_object;
      leaves.transform = transform;
      leaves.local_transform = local_transform;
      leaves.object_alias = root_object_alias;
      leaves.geometry_alias = root_geometry_alias;
      leaves.source_path = request.sop_path;
      leaves.velocity = window.enabled && attribute_motion(request.motion);
      leaves.deformation =
          defer_deformation_leaves && window.enabled && request.motion == NativeGeometryMotion::Deformation;
      leaves.sampled = leaves.deformation || (window.enabled && request.motion == NativeGeometryMotion::TransformOnly);
      leaves.implicit = implicit;
      leaves.ambiguous = ambiguous;
      leaves.sample_step = poses ? int(packed_sample_index) : -1;
      if (leaves.sampled) {
        leaves.window = window;
        leaves.source_path = request.sop_path;
      }
      if (leaves.velocity) {
        leaves.source_path = request.sop_path;
        leaves.window = window;
        if (velocity_locals)
          leaves.local_motion = *velocity_locals;
      }
      std::map<std::tuple<std::int64_t, NativeMaterialAssignment, std::string, int>, std::size_t> groups;
      bool all_leaves = ((!window.enabled || leaves.velocity) && !poses) || leaves.sampled;
      if (all_leaves)
        all_leaves = std::none_of(instances.begin(), instances.end(),
                                  [](const auto &row) { return !row.blender_reference.empty(); });
      if (all_leaves)
        for (const auto &[id, prototype] : captured_instances->prototypes) {
          auto [leaf, inserted] = leaf_prototypes.try_emplace(id, false);
          if (inserted)
            leaf->second = partition_cache_.inspect(*prototype->get()).find(NativeGeometryKind::Packed) == nullptr;
          if (!leaf->second) {
            all_leaves = false;
            break;
          }
        }
      if (all_leaves) {
        const auto routes = material_context && !poses ? material_context->pose_source() : nullptr;
        leaves.grouping = host_phase("host_packed_groups", [&] {
          return packed_grouping_cache_->prepare(source, request, captured_instances, binding_route, routes, budget);
        });
        leaves.group_indices.resize(leaves.grouping->groups.size());
        const auto &grouped = *leaves.grouping;
        if (!poses) {
          if (grouped.procedural)
            procedural_packed_roots.insert(root_object_alias);
          index_ids += grouped.index_ids;
          if (grouped.unavailable) {
            const auto &first = instances.at(grouped.first_unavailable);
            auto [found, inserted] = unavailable_packed.try_emplace(request.sop_path, 0, *first.unavailable);
            found->second.first += grouped.unavailable;
            if (inserted)
              found->second.second.resource = component_object + "/packed:" + first.identity;
          }
        }
        if (grouped.invalid_velocity) {
          velocity_issue(*instances.at(grouped.first_velocity).invalid_velocity);
          invalid_packed_velocity.at(request.sop_path).first += grouped.invalid_velocity - 1;
        }
        if (window.enabled && !attribute_motion(request.motion) && grouped.ambiguous)
          record_issue(
              {"geometry.ambiguous_packed_identity", request.sop_path, component_object,
               "Duplicate packed name/id values cannot identify instances uniquely",
               "Keeping every instance with a local index; omitting ambiguous packed motion and retaining OBJ motion"});
      }
      // Store short local identities once; namespace/set expansion is
      // pure work. Keep even unavailable contents in this population.
      if (leaves.sampled) {
        prepared->packed_populations.push_back(
            {root_object_alias, component_object, leaves.sample_step, captured_instances});
      }
      const auto visits = leaves.grouping ? leaves.grouping->groups.size() : instances.size();
      for (std::size_t visit_index = 0; visit_index < visits; ++visit_index) {
        const auto binding_instance =
            leaves.grouping ? leaves.grouping->groups[visit_index].representative : visit_index;
        const auto multiplicity = leaves.grouping ? leaves.grouping->groups[visit_index].count : 1;
        const auto &instance = instances[binding_instance];
        BindingRouteGuard route(binding_route, {binding_instance, instance.identity, instance.index_identity});
        if (!leaves.grouping && !poses && !instance.stable_memory_source)
          procedural_packed_roots.insert(root_object_alias);
        if (!leaves.sampled && !leaves.grouping) {
          const auto population_key = component_object + "/packed:" + instance.identity;
          if (poses) {
            if (!sampled_packed_population)
              throw std::logic_error("Missing packed shutter population ledger");
            sampled_packed_population->insert(population_key);
          } else if (!attribute_motion(request.motion))
            packed_populations[root_object_alias].insert(population_key);
        }
        // Keep collapsed rows in the identity/population ledgers, but
        // omit their contents. A zero scale is an ordinary visibility
        // control, including at a shutter sample or in nested packs.
        if (!leaves.grouping && collapsed_transform(instance.transform))
          continue;
        if (!instance.blender_reference.empty()) {
          const auto *spec = request.packed_blender.value.get();
          auto [table, inserted] = blender_members.try_emplace(spec);
          if (inserted && spec)
            for (const auto &prototype : spec->prototypes)
              if (prototype.source_frame)
                table->second.emplace(std::make_pair(prototype.source_reference, *prototype.source_frame), &prototype);
          const auto found = table->second.find({instance.blender_reference, instance.blender_frame});
          if (found == table->second.end()) {
            if (!poses)
              record_issue({"geometry.unprepared_blender_reference", request.sop_path, component_object,
                            instance.blender_reference + " has no evaluated prototype at frame " +
                                std::to_string(instance.blender_frame),
                            "Unpack lazy disk or foreign containers around Blender references before rendering"});
            continue;
          }
          std::vector<UniformAttribute> fields;
          std::set<std::string> field_names;
          const auto gather = [&](const GA_AttributeDict &attributes, GA_Offset offset) {
            for (auto it = attributes.begin(GA_SCOPE_PUBLIC); !it.atEnd(); ++it) {
              const auto *field = it.attrib();
              const auto name = field->getName().toStdString();
              if (name == "P" || !field_names.insert(name).second || !field->getAIFTuple() ||
                  (field->getStorageClass() != GA_STORECLASS_FLOAT && field->getStorageClass() != GA_STORECLASS_INT) ||
                  field->getTupleSize() < 1 || field->getTupleSize() > 3)
                continue;
              fpreal32 values[3]{};
              if (!field->getAIFTuple()->get(field, offset, values, field->getTupleSize()) ||
                  !std::isfinite(values[0]) || !std::isfinite(values[1]) || !std::isfinite(values[2]))
                continue;
              UniformAttribute value;
              value.name = name;
              value.components = field->getTupleSize();
              value.value = {values[0], values[1], values[2]};
              fields.push_back(std::move(value));
            }
          };
          gather(source.primitiveAttribs(), instance.primitive_offset);
          const auto *primitive = source.getPrimitive(instance.primitive_offset);
          gather(source.pointAttribs(), primitive->getPointOffset(0));
          gather(source.attribs(), GA_DETAIL_OFFSET);
          for (const auto &member : found->second->external_members) {
            const auto member_local = multiply(instance.transform, member->transform);
            if (collapsed_transform(member_local))
              continue;
            const auto local = multiply(local_transform, member_local);
            // Blender's Object/Instancer attribute fields come first; the packed primitive's follow.
            std::vector<UniformAttribute> member_fields;
            const auto *fields_used = &fields;
            if (member->blender_object && !member->blender_object->attributes.empty()) {
              member_fields = member->blender_object->attributes;
              member_fields.insert(member_fields.end(), fields.begin(), fields.end());
              fields_used = &member_fields;
            }
            const auto key =
                poses ? std::string() : external_geometry(*member, request, instance.material_assignment, *fields_used);
            ObjectData data;
            data.key = component_object + "/packed:" + instance.identity + "/member:" + member->object_key;
            if (window.enabled && request.motion == NativeGeometryMotion::Deformation) {
              auto &motion = external_motion[data.key];
              if (motion.samples.empty())
                motion.samples.resize(window.times.size());
              if (poses)
                motion.samples.at(packed_sample_index) =
                    motion.reference == instance.blender_reference ? member : nullptr;
              else {
                motion.nominal = member;
                motion.owner = external_motion_owner;
                motion.binding = instance.material_assignment;
                motion.fields = *fields_used;
                motion.window = window;
                motion.reference = instance.blender_reference;
                if (window.nominal == window.centre)
                  motion.samples.at(window.times.size() / 2) = member;
              }
            }
            data.geometry_key = key;
            data.transform = multiply(transform, member_local);
            data.visibility = request.visibility;
            data.random_id = random_id(data.key);
            data.properties = input_request.properties;
            data.attributes = *fields_used;
            apply_blender_object(data, *member, !poses && blender_first(root_object_alias, *member));
            if (!data.properties.cryptomatte_object.empty())
              data.properties.cryptomatte_object += "/packed:" + instance.identity;
            if (poses) {
              if (pose_locals)
                pose_locals->emplace(data.key, local);
            } else {
              desired.geometry.insert(key);
              desired.geometry_details[key];
              prepared->origins->emplace(data.key, NativeNavigationState::Origin{root_object_alias, local});
              if (window.enabled) {
                nominal_local_transforms[data.key] = local;
                bool invalid = false;
                auto motion = packed_velocity_samples(local_transform, velocity_locals, member_local, instance.velocity,
                                                      window, desired.navigation->roots.at(root_object_alias).motion,
                                                      invalid, {}, {}, instance.acceleration);
                if (!motion.empty())
                  packed_velocity_locals.emplace(data.key, std::move(motion));
                if (implicit || instance.index_identity)
                  implicit_objects.insert(data.key);
                if (ambiguous || instance.ambiguous_identity)
                  ambiguous_objects.insert(data.key);
              }
              ++packed_instances;
            }
            object(std::move(data));
          }
          continue;
        }
        if (instance.unavailable) {
          // Missing shutter samples are handled by motion recovery.
          // Aggregate nominal failures per SOP, including nested copies.
          if (!poses) {
            auto [found, inserted] = unavailable_packed.try_emplace(request.sop_path, 0, *instance.unavailable);
            ++found->second.first;
            if (inserted)
              found->second.second.resource = component_object + "/packed:" + instance.identity;
          }
          continue;
        }
        if (!instance.prototype)
          throw std::logic_error("Packed instance has neither owned geometry nor a source diagnostic");
        const auto &identity = instance.identity;
        const auto *detail = instance.prototype->get();
        if (!leaves.grouping && instance.invalid_velocity)
          velocity_issue(*instance.invalid_velocity);
        if (!leaves.grouping && instance.index_identity && !poses)
          ++index_ids;
        if (!leaves.grouping && window.enabled && !attribute_motion(request.motion) && instance.ambiguous_identity)
          record_issue(
              {"geometry.ambiguous_packed_identity", request.sop_path, component_object,
               "Duplicate packed name/id values cannot identify instances uniquely",
               "Keeping every instance with a local index; omitting ambiguous packed motion and retaining OBJ motion"});
        const auto descendant = leaves.grouping ? leaves.grouping->groups[visit_index].stylesheet
                                                : descendant_styles(request, source, instance.primitive_offset);
        const auto *point_prototype =
            request.point_instancing && instance.point_prototype >= 0
                ? request.point_instancing->prototypes.at(std::size_t(instance.point_prototype)).request.get()
                : nullptr;
        const auto child_request = [&] {
          auto child = point_prototype ? point_prototype_request(request, *point_prototype) : request;
          child.properties = input_request.properties;
          // A subnet represents several objects, not one object with a single priority.
          // Packed/point attributes still override this default downstream.
          if (point_prototype && input_request.point_instancing &&
              input_request.point_instancing->mode == NativeInstancingMode::Members)
            child.properties.dielectric_priority = point_prototype->properties.dielectric_priority;
          if (request.material_mode == NativeMaterialMode::Native) {
            child.set_material_assignment(instance.material_assignment);
            child.stylesheet = descendant;
            child.native_styles.reset();
            child.object_material_samples.clear();
          }
          return child;
        };
        // Content-addressed: a SOP recook (shutter sampling, a frame change)
        // gives identical prototypes new GU detail ids. Their geometry keys,
        // and so the instances using them, stay unchanged.
        const auto canonical = canonical_prototype(*detail);
        const auto prototype_key = [&] {
          return "packed:detail:" + std::to_string(canonical) +
                 (request.material_mode == NativeMaterialMode::Native ? std::string{}
                                                                      : "/material:" + request.material_key) +
                 "/smooth:" + std::to_string(point_prototype ? point_prototype->smooth : request.smooth);
        };
        if (((!window.enabled || leaves.velocity) && !poses) || leaves.sampled) {
          auto [leaf, inserted] = leaf_prototypes.try_emplace(detail->getUniqueId(), false);
          if (inserted)
            leaf->second = partition_cache_.inspect(*detail).find(NativeGeometryKind::Packed) == nullptr;
          if (leaf->second) {
            // A subnet member's object state is distinct even when it
            // shares geometry and material with another member.
            const auto child_profile =
                material_context && !poses
                    ? host_phase("host_motion_profile", [&]() { return material_context->profile(binding_route); })
                    : nullptr;
            const auto group_key = std::make_tuple(
                canonical,
                request.material_mode == NativeMaterialMode::Native ? instance.material_assignment
                                                                    : request.material_assignment(),
                (child_profile ? child_profile->signature : std::string{}) + descendant,
                input_request.point_instancing && input_request.point_instancing->mode == NativeInstancingMode::Members
                    ? instance.point_prototype
                    : -1);
            auto [found, new_group] = groups.try_emplace(group_key, leaves.groups.size());
            if (new_group) {
              auto &group = leaves.groups.emplace_back();
              group.first_component = prepared->components.size();
              auto child = child_request();
              object_templates = &group.objects;
              deformation_templates = leaves.deformation ? &group.samples : nullptr;
              visit(*detail, child, prototype_key(), "", identity_matrix, depth + 1, identity_matrix, false, false,
                    nullptr, style_time);
              object_templates = nullptr;
              deformation_templates = nullptr;
              group.last_component = prepared->components.size();
              for (auto n = group.first_component; n < group.last_component; ++n) {
                const auto &component = prepared->components[n];
                if (component.selection->kind != NativeGeometryKind::Volume &&
                    std::any_of(component.request.shader_attributes.begin(), component.request.shader_attributes.end(),
                                [&](const auto &name) { return !named_attribute(*component.source, name, true); }))
                  group.instance_attributes = true;
              }
              if (!leaves.grouping && leaves.placements.empty())
                leaves.placements.reserve(instances.size());
            }
            leaves.groups[found->second].count += multiplicity;
            if (leaves.grouping)
              leaves.group_indices[visit_index] = found->second;
            else
              leaves.placements.push_back({binding_instance, found->second});
            continue;
          }
        }
        if (leaves.grouping)
          throw std::logic_error("Acquired leaf group lost its prototype classification");
        auto child = child_request();
        if (!child.properties.cryptomatte_object.empty())
          child.properties.cryptomatte_object += "/packed:" + identity;
        bool invalid = false;
        auto local_motion =
            packed_velocity_samples(local_transform, velocity_locals, instance.transform, instance.velocity, window,
                                    desired.navigation->roots.at(root_object_alias).motion, invalid,
                                    instance.angular_velocity, instance.pivot, instance.acceleration);
        if (invalid)
          velocity_issue(
              {"motion.packed_velocity", request.sop_path, component_object + "/packed:" + identity,
               "Packed velocity produces an unrepresentable shutter transform (non-finite, or w turns half a "
               "turn or more between two motion steps; raise Motion Samples)",
               "Rendering this instance without its packed velocity; retaining independent parent, OBJ and "
               "prototype motion"});
        const auto parent_source = instance_source;
        instance_source = std::make_shared<PreparedScene::InstanceSource>(
            PreparedScene::InstanceSource{&source,
                                          instance.primitive_offset,
                                          request.sop_path,
                                          parent_source,
                                          {binding_instance, instance.identity, instance.index_identity},
                                          instances.size()});
        visit(*detail, child, prototype_key(), component_object + "/packed:" + identity,
              multiply(transform, instance.transform), depth + 1, multiply(local_transform, instance.transform),
              implicit || instance.index_identity, ambiguous || instance.ambiguous_identity,
              local_motion.empty() ? nullptr : &local_motion, style_time);
        instance_source = parent_source;
      }
      if (leaves.grouping || !leaves.placements.empty()) {
        for (const auto &group : leaves.groups) {
          if (leaves.deformation && !poses && !group.samples.empty())
            deferred_deformation_roots.insert(root_object_alias);
          for (auto n = group.first_component; n < group.last_component; ++n)
            prepared->components[n].multiplicity = leaves.sampled ? 0 : group.count;
        }
        prepared->packed_leaves.push_back(std::move(leaves));
      }
    }
    ancestors.erase(source.getUniqueId());
  };
  std::map<std::string, std::size_t> source_requests;
  for (const auto &request : requests)
    ++source_requests[request.source_key()];
  NativeExternalBindings external_bindings;
  const auto packed_population_request = [](const GeometryRequest &request) {
    auto result = request;
    // Packed references always carry an explicit sourceFrame on the primitive.
    // The table's point-instancing fallback frame is never consumed here.
    if (result.packed_blender) {
      auto table = std::make_shared<NativePointInstancing>(*result.packed_blender);
      table->frame = 0;
      table->frame_samples.clear();
      result.packed_blender.value = std::move(table);
    }
    return result;
  };
  const NativeExternalBindings::Context external_context{
      time,
      next_revision_,
      material_paths_,
      stylesheet_resources_,
      prepare_materials,
      record_issue,
      [&](const std::string &key, const std::shared_ptr<const MeshData> &source) -> std::shared_ptr<const MeshData> {
        const auto found = cache_.find(key);
        const bool same = found != cache_.end() && found->second.external_mesh &&
                          (found->second.external_mesh == source || (source->key.starts_with("blend/motion/") &&
                                                                     found->second.external_mesh->key == source->key));
        return same ? found->second.external_render_mesh : nullptr;
      },
      [&](const std::string &key, NativeExternalBindings::Mesh value) {
        prepared->external_meshes.try_emplace(key, PreparedScene::ExternalMesh{std::move(value.source),
                                                                               std::move(value.rendered),
                                                                               std::move(value.references)});
      }};
  external_geometry = [&](const GeometryRequest &member, const GeometryRequest &owner,
                          const NativeMaterialAssignment &point_binding, const std::vector<UniformAttribute> &fields) {
    auto key = external_bindings.resolve(member, owner, point_binding, fields, external_context);
    if (capture_packed_bindings && packed_population) {
      AttributeLayout layout;
      for (const auto &field : fields)
        layout.emplace(field.name, field.components);
      if (packed_binding_states.emplace(&member, point_binding, std::move(layout)).second) {
        // This is a small current palette, never an unbounded per-instance
        // history. Complex populations retain the ordinary traversal path.
        if (packed_population->bindings.size() == 256)
          packed_population.reset();
        else {
          auto fields_layout = fields;
          for (auto &field : fields_layout) {
            field.value = {};
            field.motion_values.clear();
          }
          packed_population->bindings.push_back({member, point_binding, std::move(fields_layout), key});
        }
      }
    }
    return key;
  };
  if (!roots &&
      std::any_of(requests.begin(), requests.end(), [](const auto &request) { return bool(request.point_instancing); }))
    point_instance_cache_->begin_capture();
  for (const auto &request : requests) {
    external_motion_owner = &request;
    diagnostic_root = request.object_key;
    defer_deformation_leaves = source_requests.at(request.source_key()) == 1;
    const auto initial_packed = packed_instances, initial_index = index_ids;
    window = request.motion_blur ? shutter : native_motion_window(time, NativeMotionSettings{});
    if (request.external_mesh) {
      const auto key = external_geometry(request, request, {}, {});
      ObjectData data;
      data.key = request.object_key;
      data.geometry_key = key;
      data.transform = request.transform;
      data.visibility = request.visibility;
      data.random_id = request.random_id;
      data.properties = request.properties;
      object(std::move(data));
      desired.geometry.insert(key);
      desired.geometry_details[key];
      desired.geometry_aliases[request.geometry_key].insert(key);
      desired.object_aliases[request.object_key].insert(request.object_key);
      auto &navigation = desired.navigation->roots[request.object_key];
      navigation.nominal = navigation.centre = request.transform;
      navigation.window = window;
      prepared->origins->emplace(request.object_key,
                                 NativeNavigationState::Origin{request.object_key, identity_matrix, {}});
      continue;
    }
    if (request.sop_path.empty() || request.geometry_key.empty() || request.object_key.empty())
      throw std::invalid_argument("Native geometry request requires SOP and stable resource/object keys");
    if (roots && roots->inputs.at(request.source_key()).diagnostic) {
      for (const auto &issue : roots->inputs.at(request.source_key()).warnings)
        record_issue(issue);
      record_issue(*roots->inputs.at(request.source_key()).diagnostic);
      continue;
    }
    auto *sop_node = OPgetDirector()->findSOPNode(request.sop_path.c_str());
    if (!sop_node) {
      record_issue({"geometry.missing_source", request.sop_path, request.geometry_key,
                    "The requested SOP no longer exists", "Omitting this source; rendering other objects"});
      continue;
    }
    // Every cook of this request's source, at any time, goes through one root source.
    NativeRootSource root_source(*sop_node, request, point_instance_cache_.get(), time, &stats_, record_issue);
    auto *sop = &root_source;
    capture_sop = sop;
    material_context.reset();
    binding_route.clear();
    std::shared_ptr<const PreservedDetail> retained_source;
    if (roots) {
      const auto &input = roots->inputs.at(request.source_key());
      retained_source = input.detail;
      for (const auto &issue : input.warnings)
        record_issue(issue);
    } else {
      const auto started = Clock::now();
      OP_Context context(time);
      const auto handle = sop->getCookedGeoHandle(context);
      stats_.cooking_seconds += elapsed(started);
      if (!handle.gdp() || sop->error() >= UT_ERROR_ABORT) {
        record_issue({"geometry.cook_failed", request.sop_path, request.geometry_key, "SOP cooking failed",
                      "Omitting this source; rendering other objects"});
        continue;
      }
      retained_source = std::make_shared<PreservedDetail>(handle);
    }
    // Evaluating a shader parameter can itself trigger a SOP cook. Keep
    // this acquired source immutable while host graph callbacks run.
    material_context = host_phase("host_material_motion", [&]() {
      return material_motion_cache_->prepare(*sop, request, window, retained_source, packed_ledger, prepare_materials,
                                             stats_, binding_cache_, partition_cache_, style_cache_,
                                             style_binding_cache_, *packed_preparation_cache_, budget,
                                             host_profiling ? &stats_.packed_assembly_seconds : nullptr);
    });
    if (material_context) {
      retained_source = material_context->nominal();
      const auto &paths = material_context->dependencies();
      material_paths_.insert(paths.begin(), paths.end());
      const auto &styles = material_context->stylesheet_resources();
      stylesheet_resources_.insert(styles.begin(), styles.end());
    }
    const auto *source = retained_source->get();
    packed_sources.retain(retained_source);
    prepared->roots.push_back(std::move(retained_source));
    // Packed pose assembly can consume the very same immutable shutter
    // roots as sampled fields. Seed its acquisition table before it visits
    // them, otherwise pose sampling would cook and invalidate the field
    // stamp before the field reuse check is reached.
    if (const auto found = active_.instance_fields.find(request.object_key); found != active_.instance_fields.end()) {
      const auto &value = found->second;
      auto probe = request;
      probe.shader_attributes = value.names;
      if (value.source == request.sop_path &&
          value.identifiers == deformation_identifiers(*source, *sop, probe, window, NativeGeometryKind::Packed)) {
        auto &samples = packed_shutter_roots[request.source_key()];
        if (samples.empty())
          samples.resize(window.times.size());
        for (std::size_t step = 0; step < window.times.size(); ++step) {
          auto &acquired = roots ? roots->shutter_inputs[{request.source_key(), window.times[step]}] : samples[step];
          if (!acquired.acquired) {
            acquired.acquired = true;
            acquired.detail = value.shutter->samples.at(step)->detail;
          }
        }
      }
    }
    const auto base_transform = window.enabled && !request.object_path.empty()
                                    ? capture_object_transform(request.object_path, window.centre)
                                    : request.transform;
    auto &navigation_root = desired.navigation->roots[request.object_key];
    navigation_root.path = request.object_path;
    navigation_root.window = window;
    navigation_root.nominal = request.transform;
    navigation_root.centre = base_transform;
    if (window.enabled)
      for (const auto sample_time : window.times)
        navigation_root.motion.push_back(request.object_path.empty()
                                             ? request.transform
                                             : capture_object_transform(request.object_path, sample_time));
    const auto point_sample = roots ? roots->inputs.at(request.source_key()).point_sample : root_source.point_sample();
    // Only one flat external packed root can certify the entire accepted
    // population. A preserve lease proves every packed intrinsic, identity and
    // numeric field (including fields not currently requested by a shader).
    // Nested/procedural/mixed geometry, styles and all motion stay on the full
    // path. Material graphs are deliberately re-resolved below at current time.
    const bool flat_external_candidate =
        requests.size() == 1 && roots && request.packed_blender && !point_sample && !window.enabled &&
        request.render_types.at(static_cast<std::size_t>(NativeGeometryKind::Packed)) && request.stylesheet.empty() &&
        !request.native_styles && request.object_material_samples.empty() &&
        !source->findPrimitiveAttribute("material_stylesheet") && !source->findPointAttribute("material_stylesheet") &&
        !source->findGlobalAttribute("material_stylesheet");
    const auto *flat_packed =
        flat_external_candidate ? partition_cache_.inspect(*source).find(NativeGeometryKind::Packed) : nullptr;
    if (flat_packed && flat_packed->full_detail) {
      auto proof_request = packed_population_request(request);
      const auto &accepted = active_.packed_population;
      if (accepted && accepted->detail->get() == source && accepted->request == proof_request && active_.navigation &&
          active_.navigation->origins && active_.objects.size() == accepted->objects &&
          active_.packed_deformations.empty() && active_.instance_fields.empty() &&
          active_.unavailable_geometry.empty()) {
        bool reusable = true;
        for (const auto &binding : accepted->bindings)
          reusable &= external_geometry(binding.member, request, binding.assignment, binding.fields) == binding.key;
        reusable &= prepared->external_meshes.size() == accepted->meshes.size();
        for (const auto &[key, value] : prepared->external_meshes) {
          const auto found = accepted->meshes.find(key);
          reusable &= found != accepted->meshes.end() && found->second.first == value.source &&
                      found->second.second == value.mesh;
        }
        if (reusable && prepared->diagnostics.empty()) {
          desired.geometry = active_.geometry;
          desired.geometry_details = active_.geometry_details;
          desired.geometry_aliases = active_.geometry_aliases;
          desired.binding_details = active_.binding_details;
          desired.partition_details = active_.partition_details;
          desired.packed_population = *accepted;
          desired.navigation->origins = active_.navigation->origins;
          prepared->packed_population_reuse = true;
          prepared->packed_instances = accepted->instances;
          prepared->index_ids = accepted->index_ids;
          continue;
        }
      }
      packed_population.emplace();
      packed_population->detail = prepared->roots.back();
      packed_population->request = std::move(proof_request);
    }
    if (point_sample && !point_sample->direct.empty() && !window.enabled) {
      PreparedScene::DirectInstances batch;
      batch.sample = point_sample;
      batch.root = request.object_key;
      batch.geometry_alias = request.geometry_key;
      batch.transform = base_transform;
      batch.model.visibility = request.visibility;
      batch.model.properties = request.properties;
      host_phase("host_direct_bindings", [&]() {
        for (const auto index : point_sample->direct_bindings) {
          const auto &placement = point_sample->direct.at(index);
          auto key = external_geometry(*placement.request, request, placement.binding, placement.attributes);
          desired.geometry.insert(key);
          desired.geometry_details[key];
          desired.geometry_aliases[request.geometry_key].insert(key);
          batch.bindings.push_back(std::move(key));
        }
        return 0;
      });
      prepared->direct_instances.push_back(std::move(batch));
    } else if (point_sample && !point_sample->direct.empty()) {
      std::vector<std::shared_ptr<const NativePointInstanceSample>> sampled;
      std::vector<std::map<std::string, const NativePointInstanceSample::DirectPlacement *>> sampled_rows;
      if (window.enabled && request.motion == NativeGeometryMotion::Deformation) {
        for (const auto sample_time : window.times) {
          sampled.push_back(
              point_instance_cache_->synthesize(*sop_node, request, sample_time, point_sample.get(), &stats_));
          auto &rows = sampled_rows.emplace_back();
          for (const auto &row : sampled.back()->direct)
            rows.emplace(row.identity, &row);
          for (const auto &issue : sampled.back()->diagnostics)
            record_issue(issue);
        }
      }
      // One collection member can occur on thousands of template points. Its
      // geometry binding depends on the point material and field layout, while
      // the values and transforms still belong to each individual object.
      struct MemberBinding {
        const NativePointInstanceSample::DirectPlacement *previous{};
        std::string geometry_key;
      };
      std::map<const GeometryRequest *, MemberBinding> member_bindings;
      auto &geometry_aliases = desired.geometry_aliases[request.geometry_key];
      auto &object_aliases = desired.object_aliases[request.object_key];
      for (const auto &placement : point_sample->direct) {
        auto &cached = member_bindings[placement.request.get()];
        const auto same_field = [](const UniformAttribute &left, const UniformAttribute &right) {
          return left.name == right.name && left.components == right.components;
        };
        if (!cached.previous || cached.previous->binding != placement.binding ||
            !std::ranges::equal(cached.previous->attributes, placement.attributes, same_field)) {
          cached.geometry_key = external_geometry(*placement.request, request, placement.binding, placement.attributes);
          cached.previous = &placement;
          desired.geometry.insert(cached.geometry_key);
          desired.geometry_details[cached.geometry_key];
          geometry_aliases.insert(cached.geometry_key);
        }
        ObjectData data;
        data.key = request.object_key + "/external:" + placement.identity;
        if (!sampled_rows.empty()) {
          auto &motion = external_motion[data.key];
          motion.nominal = placement.request;
          motion.owner = &request;
          motion.binding = placement.binding;
          motion.fields = placement.attributes;
          motion.window = window;
          for (const auto &rows : sampled_rows) {
            const auto found = rows.find(placement.identity);
            const auto &prototypes = request.point_instancing->prototypes;
            const bool matches = found != rows.end() && found->second->prototype >= 0 && placement.prototype >= 0 &&
                                 std::size_t(found->second->prototype) < prototypes.size() &&
                                 std::size_t(placement.prototype) < prototypes.size() &&
                                 prototypes[found->second->prototype].source_reference ==
                                     prototypes[placement.prototype].source_reference;
            motion.samples.push_back(matches ? found->second->request : nullptr);
          }
        }
        data.geometry_key = cached.geometry_key;
        data.transform = multiply(base_transform, placement.transform);
        data.visibility = request.visibility;
        data.random_id = random_id(data.key);
        data.properties = request.properties;
        data.attributes = placement.attributes;
        apply_blender_object(data, *placement.request, blender_first(request.object_key, *placement.request));
        if (!data.properties.cryptomatte_object.empty())
          data.properties.cryptomatte_object += "/external:" + placement.identity;
        std::vector<Matrix> locals;
        bool complete = true;
        if (window.enabled) {
          locals.reserve(window.times.size());
          for (std::size_t step = 0; step < window.times.size(); ++step) {
            auto local = placement.transform;
            if (request.motion == NativeGeometryMotion::Deformation) {
              const auto found = sampled_rows[step].find(placement.identity);
              const auto same_reference = [&](int slot) {
                const auto &prototypes = request.point_instancing->prototypes;
                return slot >= 0 && std::size_t(slot) < prototypes.size() && placement.prototype >= 0 &&
                       std::size_t(placement.prototype) < prototypes.size() &&
                       prototypes[slot].source_reference == prototypes[placement.prototype].source_reference;
              };
              if (found == sampled_rows[step].end() || !same_reference(found->second->prototype)) {
                complete = false;
                break;
              }
              local = found->second->transform;
            } else if (attribute_motion(request.motion)) {
              const auto &v = placement.velocity;
              const double delta = window.deltas[step];
              if (request.motion == NativeGeometryMotion::Acceleration) {
                const auto &a = placement.acceleration;
                local[3] = float(velocity_position(local[3], v.x, delta, a.x));
                local[7] = float(velocity_position(local[7], v.y, delta, a.y));
                local[11] = float(velocity_position(local[11], v.z, delta, a.z));
              } else {
                local[3] += float(v.x * delta);
                local[7] += float(v.y * delta);
                local[11] += float(v.z * delta);
              }
            }
            locals.push_back(local);
          }
          if (!complete) {
            locals.assign(window.times.size(), placement.transform);
            record_issue({"motion.external_instance_population", request.sop_path, data.key,
                          "External point instance membership changes during the shutter",
                          "Holding its requested-frame placement; retaining parent transform motion"});
          }
          for (std::size_t step = 0; step < locals.size(); ++step)
            data.motion.push_back(multiply(navigation_root.motion[step], locals[step]));
          data.transform = data.motion.at(data.motion.size() / 2);
          if (std::all_of(data.motion.begin(), data.motion.end(),
                          [&](const auto &matrix) { return matrix == data.transform; }))
            data.motion.clear();
          if (window.centre != window.nominal)
            data.nominal_transform = multiply(request.transform, placement.transform);
          if (complete && !sampled_rows.empty())
            for (auto &field : data.attributes) {
              for (const auto &rows : sampled_rows) {
                const auto *row = rows.at(placement.identity);
                const auto found = std::find_if(row->attributes.begin(), row->attributes.end(), [&](const auto &value) {
                  return value.name == field.name && value.components == field.components;
                });
                if (found == row->attributes.end()) {
                  field.motion_values.clear();
                  field.motion_complete = false;
                  break;
                }
                field.motion_values.push_back(found->value);
              }
            }
        }
        prepared->origins->emplace(
            data.key, NativeNavigationState::Origin{request.object_key, placement.transform, std::move(locals)});
        object_aliases.insert(data.key);
        object(std::move(data));
        ++packed_instances;
      }
    }
    // Unchanged packed deformation already owns complete prototype samples
    // and local instance motion. Re-resolve its small material palette on
    // the host, then copy/rebase instance tables only after converter handoff.
    // A source edit, shader field demand or binding repair takes acquisition.
    // File-backed or unknown packed procedurals are reacquired; a SOP
    // parameter/data ID alone cannot certify their external contents.
    // Several OBJ requests of one SOP share current nominal prototype IDs
    // and binding reconciliation through the normal acquisition path.
    auto retained_request = request;
    retained_request.transform = identity_matrix;
    const auto retained_root = active_.packed_deformations.find(request.object_key);
    const bool unique_source =
        source_requests.at(request.source_key()) == 1 && (!point_sample || point_sample->direct.empty());
    if (unique_source && window.enabled && request.motion == NativeGeometryMotion::Deformation &&
        retained_root != active_.packed_deformations.end() && retained_root->second.stable_packed_sources &&
        !retained_root->second.components.empty() && retained_root->second.request == retained_request &&
        retained_root->second.identifiers ==
            deformation_identifiers(*source, *sop, request, window, NativeGeometryKind::Packed)) {
      const auto &retained = retained_root->second;
      bool reusable = true;
      for (const auto &component : retained.components) {
        const auto found = cache_.find(component.key);
        if (!active_.geometry.contains(component.key) || found == cache_.end()) {
          reusable = false;
          break;
        }
        std::set<std::string> required;
        bool pointiness = !found->second.material_key.empty() && prepare_materials.has_pointiness &&
                          prepare_materials.has_pointiness(found->second.material_key, false);
        for (const auto &reference : found->second.material_references) {
          const auto current = refresh_native_material_reference(reference);
          if (!same_native_material_resource(current, reference)) {
            reusable = false;
            break;
          }
          if (prepare_materials) {
            NativeMaterialLookup candidate;
            candidate.emplace(current.assignment(), current);
            const auto names = prepare_materials(candidate);
            required.insert(names.begin(), names.end());
            pointiness |= prepare_materials.pointiness(candidate);
          }
        }
        if (reusable && prepare_materials) {
          reusable = std::includes(component.shader_attributes.begin(), component.shader_attributes.end(),
                                   required.begin(), required.end());
          if (component.kind == NativeGeometryKind::Mesh)
            reusable &= pointiness == component.pointiness;
        }
        if (!reusable)
          break;
      }
      if (reusable) {
        prepared->retained_packed_roots.push_back(request.object_key);
        for (const auto &component : retained.components) {
          retained_deformation_keys.insert(component.key);
          retained_prototypes.insert_or_assign(
              {request.sop_path, component.base_key, component.shader_attributes, component.pointiness}, component.key);
          for (const auto &reference : cache_.at(component.key).material_references) {
            const auto paths = native_material_paths(reference);
            material_paths_.insert(paths.begin(), paths.end());
          }
        }
        continue;
      }
    }
    std::vector<std::string> object_keys;
    captured_objects = &object_keys;
    root_object_alias = request.object_key;
    root_geometry_alias = request.geometry_key;
    component_plans.clear();
    leaf_prototypes.clear();
    packed_window = std::make_shared<const NativeMotionWindow>(window);
    capture_packed_bindings = bool(packed_population);
    visit(*source, request, request.geometry_key, request.object_key, base_transform, 0, identity_matrix, false, false,
          nullptr, time);
    capture_packed_bindings = false;
    captured_objects = nullptr;
    for (const auto &key : object_keys) {
      desired.object_aliases[request.object_key].insert(key);
      desired.geometry_aliases[request.geometry_key].insert(desired.objects.at(key).geometry_key);
    }
    // Deferred packed velocity leaves expand their objects on the
    // converter. Skip only this transform assembly, not independent
    // shader-field shutter acquisition later in the root request.
    if (window.enabled && (!attribute_motion(request.motion) || !object_keys.empty())) {
      std::vector<std::map<std::string, Matrix>> sampled(window.times.size());
      std::vector<std::map<std::string, Matrix>> sampled_locals(window.times.size());
      std::vector<std::set<std::string>> sampled_populations(window.times.size());
      const auto &outer_transforms = navigation_root.motion;
      const bool packed = request.render_types.at(static_cast<std::size_t>(NativeGeometryKind::Packed)) &&
                          partition_cache_.inspect(*source).find(NativeGeometryKind::Packed);
      std::vector<std::string> packed_components;
      ComponentDigest layout;
      for (const auto &key : object_keys)
        if (const auto found = packed_deformations.find(key); found != packed_deformations.end()) {
          packed_components.push_back(key);
          const auto &task = found->second;
          const auto &settings = task.nominal->input.request;
          layout.string(key);
          layout.scalar(task.kind);
          layout.scalar(settings.material_mode);
          layout.string(settings.material_key);
          layout.string(subdivision_signature(settings.subdivision));
          layout.scalar(settings.smooth);
          layout.scalar(settings.point_radius);
          layout.scalar(settings.point_render_mode);
          layout.scalar(settings.point_splat_default_material);
          layout.scalar(settings.curve_width);
          layout.scalar(settings.curve_shape);
          layout.scalar(settings.curve_segments);
          for (const auto &name : settings.shader_attributes)
            layout.string(name);
          if (settings.native_materials)
            for (const auto &[assignment, material] : *settings.native_materials) {
              layout.string(assignment.path);
              layout.string(material.key);
            }
        }
      const auto state_layout = layout.finish();
      const auto retained = active_.packed_deformations.find(request.object_key);
      const auto shared_key = [&](const std::string &object) {
        const auto &task = packed_deformations.at(object);
        return RetainedPrototypeKey{request.sop_path, prepared->components.at(task.component).key,
                                    task.nominal->input.request.shader_attributes,
                                    task.nominal->input.request.pointiness};
      };
      const bool matching_source =
          retained != active_.packed_deformations.end() &&
          retained->second.identifiers ==
              deformation_identifiers(*source, *sop, request, window, NativeGeometryKind::Packed);
      const bool matching_layout = matching_source && retained->second.layout == state_layout;
      const bool shared_layout =
          std::all_of(packed_components.begin(), packed_components.end(),
                      [&](const auto &object) { return retained_prototypes.contains(shared_key(object)); });
      // A binding-only edit can join a prototype already validated for
      // this SOP in the current acquisition. Its old root fingerprint
      // contains the previous binding and cannot validate that merge.
      // Use the current exact prototype/binding/field key instead. A
      // recovered shutter recipe still takes normal acquisition here.
      const bool shared_source =
          shared_layout && retained != active_.packed_deformations.end() && retained->second.cook_warning.empty() &&
          retained->second.recovered.empty() &&
          std::all_of(packed_components.begin(), packed_components.end(), [&](const auto &object) {
            const auto key = retained_prototypes.at(shared_key(object));
            const auto found = cache_.find(key);
            return found != cache_.end() && found->second.motion_warning.empty();
          });
      const bool retain_deformation =
          !packed_components.empty() && retained != active_.packed_deformations.end() &&
          retained->second.stable_packed_sources && (matching_layout || shared_source) && active_.navigation &&
          active_.navigation->origins &&
          std::all_of(object_keys.begin(), object_keys.end(),
                      [&](const auto &key) {
                        return active_.objects.contains(key) && active_.navigation->origins->contains(key);
                      }) &&
          std::all_of(packed_components.begin(), packed_components.end(), [&](const auto &object) {
            return cache_.contains(matching_layout ? active_.objects.at(object).geometry_key
                                                   : retained_prototypes.at(shared_key(object)));
          });
      if (retain_deformation)
        for (const auto &object : packed_components) {
          const auto &task = packed_deformations.at(object);
          auto &component = prepared->components.at(task.component);
          const auto base = component.key;
          const auto prototype = shared_key(object);
          const auto key =
              matching_layout ? active_.objects.at(object).geometry_key : retained_prototypes.at(prototype);
          desired.geometry.erase(base);
          desired.geometry.insert(key);
          desired.geometry_details[key].insert(desired.geometry_details.at(base).begin(),
                                               desired.geometry_details.at(base).end());
          component.key = key;
          desired.objects.edit().at(object).geometry_key = key;
          retained_deformation_keys.insert(key);
          retained_prototypes.insert_or_assign(prototype, key);
          packed_deformations.erase(object);
        }
      std::string cook_warning;
      if (retain_deformation)
        cook_warning = retained->second.cook_warning;
      OP_Context nominal_context(window.nominal);
      const bool static_packed = packed && request.motion == NativeGeometryMotion::TransformOnly && !material_context &&
                                 !procedural_packed_roots.contains(request.object_key) &&
                                 !sop->isTimeDependent(nominal_context);
      const bool deferred_material_poses =
          packed && material_context && !request.packed_blender && !attribute_motion(request.motion);
      const auto restore_nominal = [&](bool check_interrupt = true) {
        if (!packed || attribute_motion(request.motion) || material_context || static_packed)
          return;
        if (check_interrupt)
          check_shutter_interrupt();
        const auto started = Clock::now();
        OP_Context context(window.nominal);
        sop->getCookedGeoHandle(context);
        stats_.cooking_seconds += elapsed(started);
        if (check_interrupt)
          check_shutter_interrupt();
      };
      try {
        if (!static_packed && !deferred_material_poses)
          for (std::size_t i = 0; i < window.times.size(); ++i) {
            packed_sample_index = i;
            const auto &transform = outer_transforms[i];
            if (attribute_motion(request.motion)) {
              // Velocity uses this frame's owned packed population.
              // No shutter SOP cook or identity matching is needed.
              sampled_populations[i] = packed_populations[request.object_key];
              for (const auto &key : object_keys) {
                const auto found = packed_velocity_locals.find(key);
                const auto &local =
                    found == packed_velocity_locals.end() ? nominal_local_transforms.at(key) : found->second.at(i);
                sampled[i].emplace(key, multiply(transform, local));
                sampled_locals[i].emplace(key, local);
              }
              continue;
            }
            if (retain_deformation) {
              sampled_populations[i] = packed_populations[request.object_key];
              for (const auto &key : object_keys) {
                const auto &origin = active_.navigation->origins->at(key);
                const auto local = origin.motion.empty() ? origin.local : origin.motion.at(i);
                sampled[i].emplace(key, multiply(transform, local));
                sampled_locals[i].emplace(key, local);
              }
              continue;
            }
            if (!packed) {
              for (const auto &key : object_keys) {
                sampled[i].emplace(key, transform);
                sampled_locals[i].emplace(key, identity_matrix);
              }
              continue;
            }
            if (i == window.times.size() / 2 && window.centre == window.nominal) {
              sampled_populations[i] = packed_populations[request.object_key];
              for (const auto &key : object_keys) {
                sampled[i].emplace(key, desired.objects.at(key).transform);
                sampled_locals[i].emplace(key, nominal_local_transforms.at(key));
              }
              for (const auto &key : object_keys)
                if (auto found = packed_deformations.find(key); found != packed_deformations.end()) {
                  found->second.samples[i] = found->second.nominal;
                }
              continue;
            }
            auto &samples = packed_shutter_roots[request.source_key()];
            if (samples.empty())
              samples.resize(window.times.size());
            auto &acquired = roots ? roots->shutter_inputs[{request.source_key(), window.times[i]}] : samples.at(i);
            if (!acquired.acquired) {
              acquired.acquired = true;
              check_shutter_interrupt();
              const auto cook_start = Clock::now();
              OP_Context sample_context(window.times[i]);
              const auto sample_handle = sop->getCookedGeoHandle(sample_context);
              stats_.cooking_seconds += elapsed(cook_start);
              ++stats_.motion_cooks;
              check_shutter_interrupt();
              if (!sample_handle.gdp() || sop->error() >= UT_ERROR_ABORT)
                acquired.warning =
                    "Packed SOP shutter cook failed at time " + std::to_string(window.times[i]) + " seconds";
              else
                acquired.detail = std::make_shared<PreservedDetail>(sample_handle);
            }
            if (!acquired.warning.empty()) {
              cook_warning = acquired.warning;
              break;
            }
            if (!acquired.detail)
              throw std::logic_error("Acquired packed shutter root has neither geometry nor a source diagnostic");
            packed_sources.retain(acquired.detail);
            const auto *sample_source = acquired.detail->get();
            poses = &sampled[i];
            pose_locals = &sampled_locals[i];
            sampled_packed_population = &sampled_populations[i];
            component_plans.clear();
            leaf_prototypes.clear();
            visit(*sample_source, sample_object_materials(request, window.times[i]), request.geometry_key,
                  request.object_key, transform, 0, identity_matrix, false, false, nullptr, window.times[i]);
            poses = nullptr;
            pose_locals = nullptr;
            sampled_packed_population = nullptr;
          }
      } catch (const MotionCaptureInterrupted &) {
        throw;
      } catch (...) {
        poses = nullptr;
        pose_locals = nullptr;
        sampled_packed_population = nullptr;
        const auto error = std::current_exception();
        restore_nominal(false);
        std::rethrow_exception(error);
      }
      restore_nominal();
      const bool has_deformation =
          !packed_components.empty() || deferred_deformation_roots.contains(request.object_key);
      prepared->motion_assemblies.push_back(
          {request, window, std::move(object_keys), std::move(sampled), std::move(sampled_locals),
           std::move(packed_populations[request.object_key]), std::move(sampled_populations), cook_warning,
           retain_deformation, has_deformation});
      auto &assembly = prepared->motion_assemblies.back();
      assembly.static_packed = static_packed;
      if (deferred_material_poses)
        assembly.material_poses = material_context->pose_source();
      if (has_deformation) {
        // An acquired deformation root can have no requested-frame leaves,
        // even when its shutter samples contain packed members. Preserve that
        // empty nominal membership explicitly; shutter-only members must not
        // become objects, and alias/object lookups below must still reject
        // stale references rather than silently treating them as empty.
        desired.object_aliases.try_emplace(request.object_key);
        auto &state = desired.packed_deformations[request.object_key];
        state.layout = state_layout;
        state.cook_warning = cook_warning;
        state.request = std::move(retained_request);
        state.stable_packed_sources = !procedural_packed_roots.contains(request.object_key);
        state.packed_instances = packed_instances - initial_packed;
        state.index_ids = index_ids - initial_index;
        // Shutter cooking can replace the host's nominal detail. Stamp
        // that restored state, not the earlier leased detail or its cook
        // count, so a repeated scene update need not cook/sample again.
        check_shutter_interrupt();
        OP_Context nominal_context(window.nominal);
        const auto restored = sop->getCookedGeoHandle(nominal_context);
        check_shutter_interrupt();
        if (!restored || sop->error() >= UT_ERROR_ABORT)
          record_issue({"motion.nominal_restore", request.sop_path, request.geometry_key,
                        "The nominal SOP could not be recooked after shutter sampling",
                        "Rendering the retained requested-frame geometry; not caching this failed source evaluation"});
        else
          state.identifiers =
              deformation_identifiers(*restored.gdp(), *sop, request, window, NativeGeometryKind::Packed);
      }
    }
    // Velocity mode holds instance fields at the requested frame, as Karma and
    // Redshift do: no SOP is cooked at other shutter times.
    if (window.enabled && !window.vectors && !attribute_motion(request.motion) &&
        request.render_types[std::size_t(NativeGeometryKind::Packed)]) {
      std::set<std::string> names;
      for (const auto &component : prepared->components)
        if (component.root == request.object_key && component.selection->kind != NativeGeometryKind::Volume)
          for (const auto &name : component.request.shader_attributes)
            if (!named_attribute(*component.source, name, true))
              names.insert(name);
      const auto authored = [&](const GU_Detail &value) {
        for (const auto &name : names)
          for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_VERTEX, GA_ATTRIB_POINT, GA_ATTRIB_DETAIL})
            if (value.findAttribute(owner, name.c_str()))
              return true;
        return false;
      };
      bool demanded = !names.empty() && authored(*source);
      unsigned packed_depth{};
      for (const auto &batch : prepared->packed_leaves)
        if (batch.object_alias == request.object_key) {
          unsigned depth = 1;
          if (!demanded)
            demanded = authored(*batch.source);
          for (auto layer = batch.ancestors; layer; layer = layer->parent) {
            ++depth;
            if (!demanded)
              demanded = authored(*layer->source);
          }
          packed_depth = std::max(packed_depth, depth);
        }
      for (const auto &[key, layers] : prepared->instance_sources)
        if (prepared->origins->at(key).root == request.object_key) {
          unsigned depth{};
          for (auto layer = layers; layer; layer = layer->parent) {
            ++depth;
            if (!demanded)
              demanded = authored(*layer->source);
          }
          packed_depth = std::max(packed_depth, depth);
        }
      OP_Context nominal_context(window.nominal);
      if (demanded && (sop->isTimeDependent(nominal_context) || procedural_packed_roots.contains(request.object_key))) {
        // Transform-only scene updates must not re-cook the same
        // animated packed fields at every shutter time. Retain their
        // immutable owner hierarchy with the accepted scene. The
        // dependency stamp includes inactive branches, rewiring and
        // cook counts; changed field demand also invalidates reuse.
        // External packed procedurals cannot be certified this way.
        auto stamp_request = request;
        stamp_request.shader_attributes = names;
        const bool reusable_source = !procedural_packed_roots.contains(request.object_key);
        const auto previous = active_.instance_fields.find(request.object_key);
        if (reusable_source && previous != active_.instance_fields.end()) {
          const auto &value = previous->second;
          if (value.source == request.sop_path && value.names == names && value.depth == packed_depth &&
              value.identifiers ==
                  deformation_identifiers(*source, *sop, stamp_request, window, NativeGeometryKind::Packed)) {
            prepared->instance_field_shutters.emplace(request.object_key, *value.shutter);
            desired.instance_fields.emplace(request.object_key, value);
            continue;
          }
        }
        using Hierarchy = NativeInstanceFieldShutter::Hierarchy;
        auto &fields = prepared->instance_field_shutters[request.object_key];
        fields.window = window;
        fields.samples.reserve(window.times.size());
        std::map<std::pair<std::int64_t, unsigned>, std::shared_ptr<const Hierarchy>> memo;
        std::set<std::int64_t> visiting;
        auto field_request = request;
        field_request.material_mode = NativeMaterialMode::Uniform;
        field_request.native_styles.reset();
        field_request.native_material_policy.reset();
        std::function<std::shared_ptr<const Hierarchy>(std::shared_ptr<const PreservedDetail>, unsigned)> hierarchy;
        hierarchy = [&](std::shared_ptr<const PreservedDetail> detail,
                        unsigned depth) -> std::shared_ptr<const Hierarchy> {
          const auto id = detail->get()->getUniqueId();
          if (depth > 64 || visiting.contains(id)) {
            auto failed = std::make_shared<Hierarchy>();
            failed->warning = "Cyclic or excessive packed shutter attribute hierarchy";
            return failed;
          }
          // A shared detail can appear at different nesting depths;
          // its bounded traversal must retain the remaining route.
          const auto memo_key = std::make_pair(id, depth);
          if (const auto found = memo.find(memo_key); found != memo.end())
            return found->second;
          visiting.insert(id);
          auto result = std::make_shared<Hierarchy>();
          result->detail = std::move(detail);
          // Stop after the acquired nominal owner routes. A leaf at
          // that depth needs only attribute schema lookup, without
          // classifying/scanning its mesh primitives.
          if (depth < packed_depth) {
            const auto partitions = partition_cache_.acquire(*result->detail->get());
            if (const auto *packed = partitions->find(NativeGeometryKind::Packed)) {
              result->rows = packed_preparation_cache_->prepare(*result->detail->get(), field_request, *packed,
                                                                packed_sources, {}, false, budget, true);
              for (const auto &[child_id, child] : result->rows->prototypes)
                result->children.emplace(child_id, hierarchy(child, depth + 1));
            }
          }
          visiting.erase(id);
          memo.emplace(memo_key, result);
          return result;
        };
        const auto nominal_owner = packed_sources.preserved(source->getUniqueId());
        for (std::size_t step = 0; step < window.times.size(); ++step) {
          std::shared_ptr<const PreservedDetail> detail;
          std::string warning;
          if (window.times[step] == window.nominal)
            detail = nominal_owner;
          else if (!material_context || !material_context->sample_detail(step, detail, warning)) {
            auto &samples = packed_shutter_roots[request.source_key()];
            if (samples.empty())
              samples.resize(window.times.size());
            auto &acquired = roots ? roots->shutter_inputs[{request.source_key(), window.times[step]}] : samples[step];
            if (!acquired.acquired) {
              acquired.acquired = true;
              const auto cook = [&](double at) {
                const auto started = Clock::now();
                const auto before = sop->getCookCount();
                OP_Context context(at);
                auto handle = sop->getCookedGeoHandle(context);
                stats_.cooking_seconds += elapsed(started);
                stats_.motion_cooks += std::uint64_t(std::max<exint>(0, sop->getCookCount() - before));
                return handle;
              };
              try {
                check_shutter_interrupt();
                const auto handle = cook(window.times[step]);
                check_shutter_interrupt();
                if (handle && sop->error() < UT_ERROR_ABORT)
                  acquired.detail = std::make_shared<PreservedDetail>(handle);
                else
                  acquired.warning =
                      "Packed attribute SOP cook failed at time " + std::to_string(window.times[step]) + " seconds";
              } catch (const MotionCaptureInterrupted &) {
                throw;
              } catch (...) {
                const auto error = std::current_exception();
                cook(window.nominal);
                std::rethrow_exception(error);
              }
              check_shutter_interrupt();
              const auto restored = cook(window.nominal);
              check_shutter_interrupt();
              if (!restored || sop->error() >= UT_ERROR_ABORT)
                record_issue({"motion.nominal_restore", request.sop_path, request.geometry_key,
                              "The nominal SOP could not be recooked after instance attribute sampling",
                              "Rendering the retained requested-frame geometry and fields"});
            }
            detail = acquired.detail;
            warning = acquired.warning;
          }
          if (!detail) {
            auto failed = std::make_shared<Hierarchy>();
            failed->warning = warning.empty() ? "Packed shutter attribute source is unavailable" : warning;
            fields.samples.push_back(std::move(failed));
          } else
            fields.samples.push_back(hierarchy(std::move(detail), 0));
        }
        if (reusable_source) {
          check_shutter_interrupt();
          OP_Context context(window.nominal);
          const auto restored = sop->getCookedGeoHandle(context);
          check_shutter_interrupt();
          const auto valid = [](const auto &self, const std::shared_ptr<const Hierarchy> &value) -> bool {
            if (!value || !value->detail || !value->warning.empty())
              return false;
            for (const auto &[id, child] : value->children)
              if (!self(self, child))
                return false;
            return true;
          };
          if (restored && sop->error() < UT_ERROR_ABORT &&
              std::all_of(fields.samples.begin(), fields.samples.end(),
                          [&](const auto &value) { return valid(valid, value); })) {
            auto &retained = desired.instance_fields[request.object_key];
            retained.source = request.sop_path;
            retained.names = names;
            retained.depth = packed_depth;
            retained.identifiers =
                deformation_identifiers(*restored.gdp(), *sop, stamp_request, window, NativeGeometryKind::Packed);
            retained.shutter = std::make_shared<const NativeInstanceFieldShutter>(fields);
          }
        }
      }
    }
  }
  // Resolve one immutable motion mesh per content recipe. Each object still
  // owns independent placement and bindings, while repeated instances share
  // assembled positions/normals and the normal native geometry cache.
  std::map<std::string, std::pair<std::shared_ptr<const MeshData>, bool>> motion_meshes;
  for (auto &[object_key, motion] : external_motion) {
    if (!motion.nominal || !motion.owner || !desired.objects.contains(object_key))
      continue;
    diagnostic_root = motion.owner->object_key;
    const auto &nominal = *motion.nominal;
    if (std::all_of(motion.samples.begin(), motion.samples.end(),
                    [&](const auto &sample) { return sample && sample->external_mesh == nominal.external_mesh; }))
      continue;
    // Link hair and point clouds keep their nominal pose: deformation blur of Blender curves and
    // points is not assembled yet (docs/dev/blend-import.md "Hair", "Point clouds"); transform
    // motion still applies to the object.
    if (nominal.external_mesh && (nominal.external_mesh->external_curves || nominal.external_mesh->external_points))
      continue;
    ComponentDigest digest;
    const auto append = [&](const std::string &value) { digest.string(value); };
    append(nominal.geometry_key);
    for (const auto &sample : motion.samples)
      append(sample ? sample->geometry_key : "missing");
    append(motion.window.nominal == motion.window.centre ? "centred" : "offset");
    for (const auto delta : motion.window.deltas)
      digest.scalar(delta);
    const auto hash = digest.finish();
    const auto recipe = "blend/motion/" + std::to_string(hash[0]) + ":" + std::to_string(hash[1]);
    auto [entry, inserted] = motion_meshes.try_emplace(recipe);
    if (inserted) {
      for (const auto &[key, cached] : cache_)
        if (cached.external_mesh && cached.external_mesh->key == recipe &&
            !cached.external_mesh->motion.positions.empty()) {
          entry->second = {cached.external_mesh, false};
          break;
        }
    }
    if (!entry->second.first) {
      std::vector<std::shared_ptr<const MeshData>> samples;
      for (const auto &sample : motion.samples)
        samples.push_back(sample ? sample->external_mesh : nullptr);
      std::vector<SourceDiagnostic> issues;
      auto assembled =
          assemble_external_mesh_motion(*nominal.external_mesh, samples, motion.window, motion.owner->sop_path, issues);
      for (auto &issue : issues) {
        entry->second.second |= issue.code == "motion.external_mesh_correspondence";
        record_issue(std::move(issue));
      }
      assembled.key = recipe;
      entry->second.first = std::make_shared<const MeshData>(std::move(assembled));
    }
    const auto centre = motion.samples.empty() ? nullptr : motion.samples[motion.samples.size() / 2];
    auto member = entry->second.second || !centre ? nominal : *centre;
    member.external_mesh = entry->second.first;
    member.geometry_key = recipe;
    const auto key = external_geometry(member, *motion.owner, motion.binding, motion.fields);
    desired.objects.edit().at(object_key).geometry_key = key;
    desired.geometry.insert(key);
    desired.geometry_details[key];
    desired.geometry_aliases[motion.owner->geometry_key].insert(key);
  }
  // Nominal bindings replaced by motion meshes must not trigger spare uploads.
  std::set<std::string> used_external;
  if (prepared->packed_population_reuse)
    used_external = desired.geometry;
  for (const auto &[key, value] : desired.objects)
    used_external.insert(value.geometry_key);
  for (const auto &batch : prepared->direct_instances)
    used_external.insert(batch.bindings.begin(), batch.bindings.end());
  for (auto it = prepared->external_meshes.begin(); it != prepared->external_meshes.end();) {
    if (used_external.contains(it->first)) {
      ++it;
      continue;
    }
    desired.geometry.erase(it->first);
    desired.geometry_details.erase(it->first);
    for (auto &[alias, keys] : desired.geometry_aliases)
      keys.erase(it->first);
    it = prepared->external_meshes.erase(it);
  }
  diagnostic_root.clear();
  for (auto &[identity, entry] : nominal_samples) {
    auto &sample = *std::get<0>(entry);
    sample.identifiers = deformation_payload_identifiers(*sample.input.detail->get(), sample.input.request,
                                                         *std::get<3>(entry), std::get<2>(entry));
  }
  for (auto &[path, failure] : unavailable_packed) {
    failure.second.message += " (" + std::to_string(failure.first) + " unavailable packed instances in this source)";
    for (const auto &request : requests)
      if (request.sop_path == path)
        prepared->root_diagnostics[request.object_key].try_emplace(
            std::make_pair(failure.second.code, failure.second.source), failure.second);
    record_issue(std::move(failure.second));
  }
  // Recovery diagnostics are finalized before certification. A partial or
  // unavailable source must retry normal acquisition and retain its warnings.
  if (packed_population && !packed_population->bindings.empty() && prepared->diagnostics.empty() &&
      prepared->components.empty() && prepared->packed_leaves.empty() && prepared->packed_populations.empty() &&
      prepared->direct_instances.empty() && !desired.objects.empty()) {
    packed_population->instances = prepared->packed_instances;
    packed_population->index_ids = prepared->index_ids;
    packed_population->objects = desired.objects.size();
    for (const auto &[key, external] : prepared->external_meshes)
      packed_population->meshes.emplace(key, std::make_pair(external.source, external.mesh));
    desired.packed_population = std::move(packed_population);
  }
  return prepared;
}
void NativeSceneAdapter::assemble_packed(const std::shared_ptr<PreparedScene> &prepared) {
  const auto *profile_value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY");
  const bool profiling = profile_value && std::strcmp(profile_value, "1") == 0;
  auto phase_started = profiling ? Clock::now() : Clock::time_point{};
  const auto phase = [&](const char *name) {
    if (profiling) {
      stats_.packed_assembly_seconds[name] += elapsed(phase_started);
      phase_started = Clock::now();
    }
  };
  stats_.packed_template_components = prepared->components.size();
  using PackedDeformation = PreparedScene::PackedDeformation;
  auto &desired = prepared->desired;
  auto &packed_deformations = prepared->packed_deformations;
  const auto &retained_deformation_keys = prepared->retained_deformation_keys;
  const auto &nominal_local_transforms = prepared->nominal_locals;
  const auto &implicit_objects = prepared->implicit_objects;
  const auto &ambiguous_objects = prepared->ambiguous_objects;
  // Expand acquired leaf placements only after converter handoff. Nominal
  // and shutter templates share component/sample metadata; actual instances
  // keep independent identities, properties and complete deformation profiles.
  std::map<std::string, PreparedScene::MotionAssembly *> assemblies;
  for (auto &assembly : prepared->motion_assemblies)
    assemblies.emplace(assembly.request.object_key, &assembly);
  std::set<std::string> deferred_populations;
  // Components contain immutable capture/binding metadata. Keep one inactive
  // model here; create active components only once a complete shutter profile
  // is known. A million placements need not clone and discard that metadata.
  std::map<std::size_t, std::size_t> static_components;
  for (const auto &population : prepared->packed_populations) {
    auto &assembly = *assemblies.at(population.root);
    deferred_populations.insert(population.root);
    auto &entries = population.sample_step < 0 ? assembly.population
                                               : assembly.sampled_populations.at(std::size_t(population.sample_step));
    for (const auto &row : population.rows->values)
      entries.insert(population.prefix + "/packed:" + row.identity);
  }
  for (const auto &root : deferred_populations) {
    auto &assembly = *assemblies.at(root);
    if (assembly.window.centre == assembly.window.nominal)
      assembly.sampled_populations[assembly.window.times.size() / 2] = assembly.population;
  }
  phase("populations");
  for (const auto &batch : prepared->packed_leaves) {
    if (!batch.sampled)
      continue;
    auto &assembly = *assemblies.at(batch.object_alias);
    const auto shared_window = std::make_shared<const NativeMotionWindow>(batch.window);
    std::vector<std::vector<std::size_t>> components(batch.groups.size());
    if (batch.sample_step < 0)
      for (std::size_t group_index = 0; group_index < batch.groups.size(); ++group_index) {
        const auto &group = batch.groups[group_index];
        for (const auto &model : group.objects) {
          std::size_t index = group.first_component;
          while (index < group.last_component && prepared->components[index].key != model.geometry_key)
            ++index;
          if (index == group.last_component)
            throw std::logic_error("Packed model lost its owned component template");
          components[group_index].push_back(index);
        }
      }
    batch.each_placement([&](const NativePackedInstance &placement, std::size_t group_index) {
      const auto prefix = batch.prefix + "/packed:" + placement.identity;
      const auto transform = multiply(batch.transform, placement.transform),
                 local = multiply(batch.local_transform, placement.transform);
      const auto &group = batch.groups.at(group_index);
      for (std::size_t model_index = 0; model_index < group.objects.size(); ++model_index) {
        const auto &model = group.objects[model_index];
        auto data = model;
        data.key = prefix + model.key;
        data.transform = transform;
        data.random_id = random_id(data.key);
        if (!data.properties.cryptomatte_object.empty())
          data.properties.cryptomatte_object += "/packed:" + placement.identity;
        const auto key = data.key;
        const auto sample = group.samples.find(model.key);
        if (batch.sample_step < 0) {
          if (group.instance_attributes)
            prepared->instance_sources.emplace(
                key,
                std::make_shared<PreparedScene::InstanceSource>(PreparedScene::InstanceSource{
                    batch.source,
                    placement.primitive_offset,
                    batch.source_path,
                    batch.ancestors,
                    {std::size_t(&placement - batch.rows->values.data()), placement.identity, placement.index_identity},
                    batch.rows->values.size()}));
          const auto index = components.at(group_index).at(model_index);
          if (sample != group.samples.end()) {
            PackedDeformation task;
            task.kind = prepared->components[index].selection->kind;
            task.nominal = sample->second;
            task.window = shared_window;
            task.samples.resize(batch.window.times.size());
            task.object = key;
            task.geometry_alias = batch.geometry_alias;
            task.component = index;
            if (batch.window.centre == batch.window.nominal)
              task.samples[batch.window.times.size() / 2] = task.nominal;
            if (!packed_deformations.emplace(key, std::move(task)).second)
              throw std::logic_error("Duplicate acquired packed deformation identity");
          } else {
            auto [found, inserted] = static_components.try_emplace(index, prepared->components.size());
            if (inserted) {
              auto component = prepared->components.at(index);
              component.multiplicity = 0;
              prepared->components.push_back(std::move(component));
            }
            ++prepared->components.at(found->second).multiplicity;
          }
          if (batch.window.centre == batch.window.nominal) {
            assembly.sampled[batch.window.times.size() / 2].emplace(key, data.transform);
            assembly.locals[batch.window.times.size() / 2].emplace(key, local);
          }
          if (batch.implicit || placement.index_identity)
            prepared->implicit_objects.insert(key);
          if (batch.ambiguous || placement.ambiguous_identity)
            prepared->ambiguous_objects.insert(key);
          prepared->origins->emplace(key, NativeNavigationState::Origin{batch.object_alias, local});
          prepared->nominal_locals.emplace(key, local);
          assembly.objects.push_back(key);
          desired.object_aliases[batch.object_alias].insert(key);
          desired.geometry_aliases[batch.geometry_alias].insert(data.geometry_key);
          if (!desired.objects.emplace(key, std::move(data)).second)
            throw std::logic_error("Duplicate acquired packed object identity");
          ++prepared->packed_instances;
          if (auto found = desired.packed_deformations.find(batch.object_alias);
              found != desired.packed_deformations.end())
            ++found->second.packed_instances;
        } else {
          const auto step = std::size_t(batch.sample_step);
          if (!assembly.sampled.at(step).emplace(key, data.transform).second)
            throw std::logic_error("Duplicate acquired packed shutter identity");
          assembly.locals.at(step).emplace(key, local);
          if (sample != group.samples.end())
            if (auto found = packed_deformations.find(key); found != packed_deformations.end())
              found->second.samples.at(step) = sample->second;
        }
      }
    });
  }
  stats_.packed_expanded_components = prepared->components.size();
  stats_.packed_deformation_tasks = packed_deformations.size();
  phase("expand_instances");
  // These sources contain only owned transforms, identities and memberships.
  // Expanding per-object shutter maps belongs to conversion, not host graph
  // preparation. No OP lookup, SOP cook or material callback occurs here.
  for (auto &assembly : prepared->motion_assemblies) {
    if (!assembly.material_poses && !assembly.static_packed)
      continue;
    const auto &outer = desired.navigation->roots.at(assembly.request.object_key).motion;
    for (std::size_t step = 0; step < assembly.window.times.size(); ++step) {
      if (assembly.material_poses) {
        if (!assembly.material_poses->poses(step, assembly.request, assembly.locals[step],
                                            assembly.sampled_populations[step], assembly.cook_warning))
          break;
        for (const auto &[key, local] : assembly.locals[step])
          assembly.sampled[step].insert_or_assign(key, multiply(outer[step], local));
      } else {
        assembly.sampled_populations[step] = assembly.population;
        for (const auto &key : assembly.objects) {
          const auto &local = prepared->nominal_locals.at(key);
          assembly.locals[step].insert_or_assign(key, local);
          assembly.sampled[step].insert_or_assign(key, multiply(outer[step], local));
        }
      }
    }
  }
  phase("expand_binding_poses");
  std::string diagnostic_root;
  const auto record_issue = [&](SourceDiagnostic issue) {
    retain_diagnostic(prepared->diagnostics, issue);
    retain_diagnostic(prepared->root_diagnostics[diagnostic_root], issue);
  };
  for (const auto &assembly : prepared->motion_assemblies) {
    const auto &request = assembly.request;
    const auto &window = assembly.window;
    diagnostic_root = request.object_key;
    const auto &object_keys = assembly.objects;
    const auto &sampled = assembly.sampled;
    const auto &sampled_locals = assembly.locals;
    const auto &sampled_populations = assembly.sampled_populations;
    const auto &cook_warning = assembly.cook_warning;
    const auto &outer_transforms = desired.navigation->roots.at(request.object_key).motion;
    // An index cannot disambiguate births/deaths. Explicit identities
    // still allow unaffected instances to retain their packed motion.
    bool population_changed = false;
    for (const auto &sample : sampled_populations)
      if (sample != assembly.population) {
        population_changed = true;
        break;
      }
    std::size_t recovered = 0;
    std::vector<const Matrix *> sample_values(sampled.size());
    for (const auto &key : object_keys) {
      auto &data = desired.objects.edit().at(key);
      auto &origin = prepared->origins->at(key);
      if (window.centre != window.nominal)
        data.nominal_transform = multiply(request.transform, nominal_local_transforms.at(key));
      bool valid = cook_warning.empty() &&
                   (attribute_motion(request.motion) ||
                    (!ambiguous_objects.contains(key) && !(population_changed && implicit_objects.contains(key))));
      if (assembly.retained && active_.packed_deformations.at(request.object_key).recovered.contains(key))
        valid = false;
      if (valid)
        for (std::size_t i = 0; i < sampled.size(); ++i) {
          const auto found = sampled[i].find(key);
          if (found == sampled[i].end()) {
            valid = false;
            break;
          }
          sample_values[i] = &found->second;
          // A strand/particle may disappear during the shutter. Hold
          // its requested-frame packed pose instead of sending an
          // uninvertible motion sample to the engine. Other instances
          // and independently valid OBJ motion keep their blur.
          if (!invertible_transform(found->second) ||
              (transform_determinant(found->second) < 0) != (transform_determinant(data.transform) < 0)) {
            valid = false;
            break;
          }
        }
      if (auto found = packed_deformations.find(key); found != packed_deformations.end() && !valid)
        found->second.warning =
            cook_warning.empty()
                ? "Packed transforms, population or identities cannot be matched safely over the shutter"
                : cook_warning;
      if (valid) {
        data.transform = *sample_values[window.times.size() / 2];
        data.motion.reserve(data.motion.size() + sampled.size());
        origin.motion.reserve(origin.motion.size() + sampled.size());
        for (std::size_t i = 0; i < sampled.size(); ++i) {
          data.motion.push_back(*sample_values[i]);
          origin.motion.push_back(sampled_locals[i].at(key));
        }
      } else {
        ++recovered;
        if (assembly.deformation)
          desired.packed_deformations[request.object_key].recovered.insert(key);
        // Freeze the requested-frame packed hierarchy, preserving
        // independently valid outer OBJ motion without matrix inversion.
        const auto &local = nominal_local_transforms.at(key);
        data.transform = multiply(outer_transforms[window.times.size() / 2], local);
        data.motion.reserve(data.motion.size() + outer_transforms.size());
        for (const auto &transform : outer_transforms)
          data.motion.push_back(multiply(transform, local));
        origin.motion.assign(outer_transforms.size(), local);
      }
      if (std::all_of(data.motion.begin(), data.motion.end(),
                      [&](const auto &matrix) { return matrix == data.transform; }))
        data.motion.clear();
    }
    if (recovered)
      record_issue(
          {"motion.packed_unavailable", request.sop_path, request.geometry_key,
           (cook_warning.empty()
                ? "Packed transforms, population or identities cannot be matched safely over the shutter"
                : cook_warning) +
               " (" + std::to_string(recovered) + " components)",
           "Rendering requested-frame geometry and packed transforms; retaining independently valid OBJ motion"});
  }
  phase("motion_correspondence");
  // Geometry carries deformation as well as its bindings. Sharing the
  // nominal prototype alone is insufficient when different instance paths
  // resolve different contents at another shutter sample. Group complete
  // acquired profiles across all roots, then create only required variants.
  std::map<std::string, std::map<std::vector<std::string>, std::vector<PackedDeformation *>>> profiles;
  for (auto &[object, task] : packed_deformations) {
    std::vector<std::string> profile;
    profile.reserve(task.samples.size() * 2 + 1);
    for (const auto &sample : task.samples)
      profile.push_back(sample ? sample->key : std::string{});
    profile.push_back(task.warning);
    for (const auto sample : task.window->times)
      profile.push_back(std::to_string(std::bit_cast<std::int64_t>(sample)));
    profiles[prepared->components.at(task.component).key][std::move(profile)].push_back(&task);
  }
  phase("group_profiles");
  // GU detail identities change when an upstream prototype is edited. They
  // identify acquired samples, not the lifetime of an engine resource. Keep
  // compatible accepted resource slots so unchanged instances need no binding
  // update. Complete shutter profiles still determine the current sharing.
  const auto rendering_state = [](const std::string &key) {
    const auto marker = key.find("packed:detail:");
    if (marker == std::string::npos)
      return std::string{};
    const auto first = marker + std::strlen("packed:detail:"), last = key.find('/', first);
    if (last == std::string::npos || last == first)
      return std::string{};
    for (auto i = first; i < last; ++i)
      if (key[i] < '0' || key[i] > '9')
        return std::string{};
    return key.substr(0, first) + key.substr(last);
  };
  using RenderingState = std::pair<std::string, std::set<std::string>>;
  std::map<std::string, std::set<RenderingState>> accepted_states;
  for (const auto &[root, state] : active_.packed_deformations)
    for (const auto &component : state.components) {
      auto signature = rendering_state(component.base_key);
      if (!signature.empty())
        accepted_states[component.key].emplace(std::move(signature), component.shader_attributes);
    }
  struct ProfileGroup {
    std::string base, key;
    const std::vector<std::string> *profile;
    std::vector<PackedDeformation *> *instances;
  };
  std::vector<ProfileGroup> groups;
  std::map<std::string, std::set<std::int64_t>> profile_details;
  auto claimed = desired.geometry;
  for (auto &[base, variants] : profiles) {
    profile_details.emplace(base, desired.geometry_details.at(base));
    if (!retained_deformation_keys.contains(base))
      claimed.erase(base);
    for (auto &[profile, instances] : variants)
      groups.push_back({base, {}, &profile, &instances});
  }
  claimed.insert(retained_deformation_keys.begin(), retained_deformation_keys.end());
  struct Candidate {
    std::size_t count, group;
    std::string key;
  };
  std::vector<Candidate> candidates;
  for (std::size_t i = 0; i < groups.size(); ++i) {
    const auto &group = groups[i];
    const auto &instances = *group.instances;
    const auto signature = rendering_state(group.base);
    if (signature.empty())
      continue;
    const RenderingState state{signature, instances.front()->nominal->input.request.shader_attributes};
    std::map<std::string, std::size_t> counts;
    for (const auto *instance : instances) {
      const auto object = active_.objects.find(instance->object);
      if (object == active_.objects.end())
        continue;
      const auto &key = object->second.geometry_key;
      const auto accepted = accepted_states.find(key);
      if (accepted != accepted_states.end() && accepted->second.contains(state) && cache_.contains(key) &&
          !claimed.contains(key))
        ++counts[key];
    }
    for (auto &[key, count] : counts)
      candidates.push_back({count, i, key});
  }
  // Reserve the largest populations first. Splits cannot overwrite another
  // profile or a retained root; merges retire the slots no longer referenced.
  std::sort(candidates.begin(), candidates.end(), [](const auto &a, const auto &b) {
    if (a.count != b.count)
      return a.count > b.count;
    return std::tie(a.key, a.group) < std::tie(b.key, b.group);
  });
  for (const auto &candidate : candidates) {
    auto &group = groups[candidate.group];
    if (group.key.empty() && claimed.insert(candidate.key).second)
      group.key = candidate.key;
  }
  for (auto &group : groups)
    if (group.key.empty()) {
      auto key = group.base;
      std::size_t index = 0;
      while (!claimed.insert(key).second)
        key = group.base + "/shutter-variant:" + std::to_string(index++);
      group.key = std::move(key);
    }
  stats_.packed_profile_groups = groups.size();
  phase("assign_resource_slots");
  for (const auto &[base, variants] : profiles)
    if (!retained_deformation_keys.contains(base))
      desired.geometry.erase(base);
  for (auto &group : groups) {
    const auto &base = group.base;
    const auto &key = group.key;
    const auto &profile = *group.profile;
    auto &instances = *group.instances;
    auto &first = *instances.front();
    auto fingerprint = first.nominal->identifiers;
    ComponentDigest digest;
    bool invalid_ids = false;
    for (const auto &sample : first.samples) {
      digest.scalar(bool(sample));
      if (!sample)
        continue;
      const auto state = rendering_state(sample->key);
      digest.string(state.empty() ? sample->key : state);
      const auto identifiers = deformation_payload_identifiers(*sample->input.detail->get(), sample->input.request,
                                                               *first.window, first.kind);
      digest.scalar(identifiers.size());
      for (const auto value : identifiers)
        digest.scalar(value);
      invalid_ids |= identifiers.size() < 3 || identifiers[0] == GA_INVALID_DATAID ||
                     identifiers[1] == GA_INVALID_DATAID || identifiers[2] == GA_INVALID_DATAID;
    }
    digest.string(first.warning);
    if (invalid_ids)
      digest.scalar(prepared->serial);
    for (const auto value : digest.finish())
      fingerprint.push_back(std::bit_cast<std::int64_t>(value));
    const auto found = cache_.find(key);
    if (found == cache_.end() || found->second.identifiers != fingerprint ||
        found->second.material_key != first.nominal->input.request.material_key) {
      // Shutter recooks allocate fresh P/topology data IDs even when only an
      // outer packed-instance field changed. Compare every acquired payload
      // before publishing another geometry revision. Unsupported domains keep
      // the conservative identifier-based conversion path.
      const auto started = Clock::now();
      const auto signature = [&]() -> std::optional<std::array<std::uint64_t, 2>> {
        ComponentDigest contents;
        contents.string("packed_deformation_payload/1");
        const auto append = [&](const auto &sample) {
          contents.scalar(bool(sample));
          if (!sample)
            return true;
          if (!sample->input.detail || !sample->input.selection)
            return false;
          const auto value = component_signature(*sample->input.detail->get(), *sample->input.selection,
                                                 sample->input.request, *first.window, true);
          if (!value)
            return false;
          const auto state = rendering_state(sample->key);
          contents.string(state.empty() ? sample->key : state);
          for (auto part : *value)
            contents.scalar(part);
          return true;
        };
        if (!append(first.nominal))
          return {};
        for (const auto &sample : first.samples)
          if (!append(sample))
            return {};
        contents.string(first.warning);
        return contents.finish();
      }();
      const auto duration = elapsed(started);
      stats_.component_check_seconds += duration;
      stats_.conversion_seconds += duration;
      if (signature)
        ++stats_.component_checks;
      if (found != cache_.end() && signature && found->second.component_signature == signature &&
          found->second.material_key == first.nominal->input.request.material_key) {
        found->second.identifiers = std::move(fingerprint);
        ++stats_.component_reuses;
      } else {
        PreparedScene::Deformation sampled;
        std::vector<NativeDeformationSample> inputs;
        inputs.reserve(first.samples.size());
        for (const auto &sample : first.samples)
          inputs.push_back(sample ? sample->input : NativeDeformationSample{});
        sampled.data = prepare_deformation_samples(first.kind, first.nominal->input, std::move(inputs),
                                                   std::move(fingerprint), *first.window, first.warning);
        sampled.keys.emplace(first.kind, key);
        prepared->deformations.push_back(std::move(sampled));
      }
      cache_[key].component_signature = signature;
    }
    desired.geometry.insert(key);
    desired.geometry_details[key] = profile_details.at(base);
    std::map<std::size_t, std::size_t> resolved_components;
    for (auto *instance : instances) {
      const auto template_index = instance->component;
      if (!prepared->components.at(template_index).multiplicity) {
        auto [found, inserted] = resolved_components.try_emplace(template_index, prepared->components.size());
        if (inserted) {
          auto component = prepared->components.at(template_index);
          component.key = key;
          prepared->components.push_back(std::move(component));
        }
        instance->component = found->second;
        ++prepared->components.at(instance->component).multiplicity;
      } else
        prepared->components.at(instance->component).key = key;
      desired.objects.edit().at(instance->object).geometry_key = key;
      auto &aliases = desired.geometry_aliases.at(instance->geometry_alias);
      if (!retained_deformation_keys.contains(base))
        aliases.erase(base);
      aliases.insert(key);
    }
  }
  phase("resolve_profiles");
  std::set<std::string> rebuilt_aliases;
  for (const auto &[root, state] : desired.packed_deformations) {
    const auto &request = state.request;
    auto &aliases = desired.geometry_aliases[request.geometry_key];
    if (rebuilt_aliases.insert(request.geometry_key).second)
      aliases.clear();
    for (const auto &object : desired.object_aliases.at(request.object_key))
      aliases.insert(desired.objects.at(object).geometry_key);
  }
  std::erase_if(desired.geometry_details, [&](const auto &row) { return !desired.geometry.contains(row.first); });
  if (!packed_deformations.empty() || !retained_deformation_keys.empty() || !static_components.empty()) {
    std::map<std::pair<std::string, std::string>, std::size_t> unique;
    std::vector<PreparedScene::Component> components;
    for (auto &component : prepared->components) {
      if (!component.multiplicity)
        continue;
      if (component.window.enabled && component.request.motion == NativeGeometryMotion::Deformation) {
        auto [found, inserted] = unique.emplace(std::make_pair(component.root, component.key), components.size());
        if (!inserted) {
          components.at(found->second).multiplicity += component.multiplicity;
          continue;
        }
      }
      components.push_back(std::move(component));
    }
    prepared->components = std::move(components);
  }
  phase("collapse_components");
  stats_.packed_final_components = prepared->components.size();
  for (auto &[root, state] : desired.packed_deformations) {
    for (const auto &[key, issue] : prepared->root_diagnostics[root])
      state.diagnostics.push_back(issue);
    for (const auto &component : prepared->components)
      if (component.root == root)
        state.components.push_back({component.selection->kind, component.key, component.base_key,
                                    component.request.sop_path, component.request.shader_attributes,
                                    component.multiplicity, component.request.pointiness});
  }
  phase("retain_metadata");
}
void NativeSceneAdapter::assemble_instance_attributes(const std::shared_ptr<PreparedScene> &prepared) {
  if (prepared->instance_sources.empty() && prepared->recycled_sources.empty())
    return;
  const auto *profile_value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY");
  const bool profiling = profile_value && std::strcmp(profile_value, "1") == 0;
  auto phase_started = Clock::now();
  const auto phase = [&](const char *name) {
    if (profiling) {
      stats_.packed_assembly_seconds[name] += elapsed(phase_started);
      phase_started = Clock::now();
    }
  };
  auto &desired = prepared->desired;
  std::map<std::string, const PreparedScene::Component *> components;
  for (const auto &component : prepared->components)
    if (component.multiplicity)
      components.try_emplace(component.key, &component);
  std::set<std::string> roots, instance_field_roots;
  struct Failure {
    SourceDiagnostic issue;
    std::size_t count{};
  };
  // A nominal invalid field and an off-frame hold can coexist in one source.
  // Aggregate identical problems, never merge their reasons/fallback actions.
  std::map<std::tuple<std::string, std::string, std::string, std::string>, Failure> failures;
  struct FieldValue {
    bool present{};
    int components{};
    Float3 value{};
    std::string error;
  };
  const auto read = [](const GU_Detail &source, GA_Offset primitive_offset, const std::string &name) {
    FieldValue result;
    const auto *primitive = source.getPrimitive(primitive_offset);
    if (!primitive)
      throw std::logic_error("Packed attribute recipe lost its preserved primitive");
    const GA_Attribute *field = source.findPrimitiveAttribute(name.c_str());
    GA_Offset offset = primitive_offset;
    if (!field && primitive->getVertexCount() == 1) {
      field = source.findVertexAttribute(name.c_str());
      offset = primitive->getVertexOffset(0);
      if (!field) {
        field = source.findPointAttribute(name.c_str());
        offset = primitive->getPointOffset(0);
      }
    }
    if (!field) {
      field = source.findGlobalAttribute(name.c_str());
      offset = GA_DETAIL_OFFSET;
    }
    if (!field)
      return result;
    result.present = true;
    result.components = field->getTupleSize();
    fpreal32 value[3]{};
    if (name == "Cd" && (field->getStorageClass() != GA_STORECLASS_FLOAT || result.components != 3))
      result.error = "requires a three-component float color";
    else if ((field->getStorageClass() != GA_STORECLASS_FLOAT && field->getStorageClass() != GA_STORECLASS_INT) ||
             result.components < 1 || result.components > 3 || !field->getAIFTuple())
      result.error = "requires a numeric tuple with one, two or three components";
    else if (!field->getAIFTuple()->get(field, offset, value, result.components))
      result.error = "has no usable value on this packed instance";
    else if (!std::isfinite(value[0]) || !std::isfinite(value[1]) || !std::isfinite(value[2]))
      result.error = "contains a non-finite value";
    else
      result.value = {value[0], value[1], value[2]};
    return result;
  };
  // Every instance reads only its own preserved owners and writes only its
  // own object's fields, so instances run in parallel. Shared sets and the
  // failure aggregation are merged afterwards in key order, as before.
  using FailureKey = std::tuple<std::string, std::string, std::string, std::string>;
  struct Outcome {
    const std::string *root{};
    bool field_root{};
    std::vector<std::pair<FailureKey, SourceDiagnostic>> failures;
  };
  std::vector<std::pair<const std::string *, const std::shared_ptr<const PreparedScene::InstanceSource> *>> entries;
  if (prepared->recycled) {
    entries.reserve(prepared->recycled_sources.size());
    for (const auto &[key, source] : prepared->recycled_sources)
      entries.emplace_back(key, &source);
  } else {
    entries.reserve(prepared->instance_sources.size());
    for (const auto &[key, source] : prepared->instance_sources)
      entries.emplace_back(&key, &source);
  }
  std::vector<Outcome> outcomes(entries.size());
  auto &objects = desired.objects.edit(); // Each instance writes only its own record.
  const auto instance_fields = [&](const std::string &key,
                                   const std::shared_ptr<const PreparedScene::InstanceSource> &layers,
                                   Outcome &outcome) {
    const auto object = objects.find(key);
    if (object == objects.end())
      return;
    const auto found = components.find(object->second.geometry_key);
    if (found == components.end())
      return;
    const auto &component = *found->second;
    if (component.selection->kind == NativeGeometryKind::Volume)
      return; // Grid requirements have their own recovery rules.
    outcome.root = &component.root;
    struct SampleLayers {
      std::vector<std::pair<const GU_Detail *, GA_Offset>> layers;
      const GU_Detail *prototype{};
      std::string error;
    };
    std::vector<SampleLayers> samples;
    const auto shutter = prepared->instance_field_shutters.find(component.root);
    if (shutter != prepared->instance_field_shutters.end()) {
      std::vector<const PreparedScene::InstanceSource *> route;
      for (auto layer = layers; layer; layer = layer->parent)
        route.push_back(layer.get());
      std::reverse(route.begin(), route.end());
      samples.reserve(shutter->second.samples.size());
      for (const auto &hierarchy : shutter->second.samples) {
        auto &sample = samples.emplace_back();
        auto current = hierarchy;
        for (const auto *nominal : route) {
          if (!current || !current->warning.empty() || !current->rows) {
            sample.error = current && !current->warning.empty() ? current->warning
                                                                : "has no corresponding packed owner at a shutter time";
            break;
          }
          auto index = nominal->route.index;
          if (nominal->route.indexed) {
            if (current->rows->values.size() != nominal->population) {
              sample.error = "has changing packed counts without unambiguous correspondence";
              break;
            }
          } else {
            const auto row = current->rows->named.find(nominal->route.identity);
            if (row == current->rows->named.end()) {
              sample.error = "has no corresponding named packed owner at a shutter time";
              break;
            }
            index = row->second;
          }
          if (index >= current->rows->values.size()) {
            sample.error = "has no corresponding packed owner at a shutter time";
            break;
          }
          const auto &row = current->rows->values[index];
          sample.layers.emplace_back(current->detail->get(), row.primitive_offset);
          if (!row.prototype) {
            sample.error = "has an unavailable packed source at a shutter time";
            break;
          }
          const auto child = current->children.find(row.prototype->get()->getUniqueId());
          if (child == current->children.end())
            throw std::logic_error("Acquired packed attribute hierarchy lost its child owner");
          current = child->second;
        }
        if (sample.error.empty()) {
          if (!current || !current->warning.empty() || !current->detail)
            sample.error = current && !current->warning.empty() ? current->warning
                                                                : "has no usable packed prototype at a shutter time";
          else
            sample.prototype = current->detail->get();
        }
      }
    }
    for (const auto &name : component.request.shader_attributes) {
      // Authored prototype data wins, including invalid data: never hide
      // a broken local field by silently reading an outer value instead.
      if (named_attribute(*component.source, name, true))
        continue;
      std::size_t nominal_distance{};
      for (auto layer = layers; layer; layer = layer->parent, ++nominal_distance) {
        const auto value = read(*layer->source, layer->primitive, name);
        if (!value.present)
          continue;
        outcome.field_root = true;
        if (value.error.empty()) {
          UniformAttribute attribute{name, value.components, value.value};
          std::string error;
          for (std::size_t step = 0; step < samples.size(); ++step) {
            const auto &sample = samples[step];
            error = sample.error;
            if (error.empty() && named_attribute(*sample.prototype, name, true))
              error = "changes between prototype and instance ownership over the shutter";
            FieldValue sampled;
            if (error.empty()) {
              std::size_t distance{};
              for (auto owner = sample.layers.rbegin(); owner != sample.layers.rend(); ++owner, ++distance) {
                sampled = read(*owner->first, owner->second, name);
                if (sampled.present)
                  break;
              }
              error = !sampled.present ? "is missing at a shutter time" : sampled.error;
              if (error.empty() && distance != nominal_distance)
                error = "changes effective packed owner layer over the shutter";
              if (error.empty() && sampled.components != value.components)
                error = "changes tuple size over the shutter";
            }
            if (!error.empty()) {
              error += " (time " + std::to_string(shutter->second.window.times.at(step)) + " seconds)";
              break;
            }
            attribute.motion_values.push_back(sampled.value);
          }
          if (!error.empty()) {
            attribute.motion_values.clear();
            attribute.motion_complete = false;
            outcome.failures.emplace_back(
                FailureKey{layer->path, name, "geometry.shutter_instance_attribute", error},
                SourceDiagnostic{"geometry.shutter_instance_attribute", layer->path, key,
                                 "Packed attribute '" + name + "' " + error,
                                 "Holding the valid requested-frame field over the shutter; retaining independent "
                                 "geometry, camera and object motion",
                                 "warning", name});
          } else if (!attribute.motion_values.empty()) {
            const auto bits = [](const Float3 &v) {
              return std::array{std::bit_cast<std::uint32_t>(v.x), std::bit_cast<std::uint32_t>(v.y),
                                std::bit_cast<std::uint32_t>(v.z)};
            };
            if (std::all_of(attribute.motion_values.begin(), attribute.motion_values.end(),
                            [&](const auto &sample) { return bits(sample) == bits(attribute.value); }))
              attribute.motion_values.clear();
          }
          object->second.attributes.push_back(std::move(attribute));
        } else {
          const auto code = name == "Cd" ? "geometry.invalid_color" : "geometry.invalid_attribute";
          outcome.failures.emplace_back(
              FailureKey{layer->path, name, code, value.error},
              SourceDiagnostic{
                  code, layer->path, key, "Packed attribute '" + name + "' " + value.error,
                  "Ignoring this instance field; retaining geometry and independent shading/displacement branches",
                  "warning", name});
        }
        break; // An invalid nearer field must not select an ancestor.
      }
    }
  };
  parallel_slots(entries.size(), 256, [&](std::size_t begin, std::size_t end) {
    for (std::size_t index = begin; index < end; ++index)
      instance_fields(*entries[index].first, *entries[index].second, outcomes[index]);
  });
  for (auto &outcome : outcomes) {
    if (outcome.root)
      roots.insert(*outcome.root);
    if (outcome.field_root)
      instance_field_roots.insert(*outcome.root);
    for (auto &[failure, issue] : outcome.failures) {
      auto [found, inserted] = failures.try_emplace(std::move(failure));
      if (inserted)
        found->second.issue = std::move(issue);
      ++found->second.count;
    }
  }
  phase("convert_attributes_read");
  // Authored outer instance fields must be reacquired after an attribute edit.
  // Merely searching for a missing field does not make an in-memory source
  // unstable: keep its geometry recipe and persistent local recovery warning.
  // SOP cook/dependency stamps still invalidate the recipe when it is repaired.
  for (const auto &root : instance_field_roots)
    if (auto found = desired.packed_deformations.find(root); found != desired.packed_deformations.end())
      found->second.stable_packed_sources = false;
  using Layouts = std::map<AttributeLayout, std::uint64_t>;
  std::map<std::pair<std::string, std::string>, Layouts> populations;
  std::map<std::string, std::map<AttributeLayout, std::string>> variants;
  const auto suffix = [](const AttributeLayout &layout) {
    if (layout.empty())
      return std::string{};
    std::string result = "/instance-fields:";
    for (const auto &[name, size] : layout)
      result += std::to_string(name.size()) + ":" + name + ":" + std::to_string(size) + ";";
    return result;
  };
  const auto variant_key = [&](const std::string &key, const AttributeLayout &layout) {
    auto base = key;
    if (const auto found = cache_.find(key); found != cache_.end() && !found->second.instance_attributes.empty()) {
      const auto previous = suffix(found->second.instance_attributes);
      if (!base.ends_with(previous))
        throw std::logic_error("Packed instance declaration lost its owned resource suffix");
      base.resize(base.size() - previous.size());
    }
    return base + suffix(layout);
  };
  std::set<std::string> sampled;
  for (const auto &task : prepared->deformations)
    for (const auto &[kind, key] : task.keys)
      sampled.insert(key);
  // Per-object layouts and roots are pure reads: compute them in parallel.
  // The variant key of a (component, layout) pair cannot change during this
  // walk (only component signatures are written to the cache), so each
  // distinct (root, component, layout) is resolved once, at its first object
  // in key order, exactly where the per-object walk first resolved it.
  // An object whose attribute list equals (name and size, in order) the
  // previous object's in its slot range has the same layout: share it rather
  // than building one map per object. Equal lists give equal layouts.
  struct Placement {
    std::pair<const std::string, ObjectData> *entry{};
    const std::string *root{};
    std::shared_ptr<const AttributeLayout> layout;
    std::size_t combination{};
  };
  const auto same_fields = [](const std::vector<UniformAttribute> &a, const std::vector<UniformAttribute> &b) {
    return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](const auto &x, const auto &y) {
             return x.components == y.components && x.name == y.name;
           });
  };
  std::vector<Placement> placed;
  placed.reserve(desired.objects.size());
  for (auto &entry : desired.objects.edit())
    placed.push_back({&entry});
  parallel_slots(placed.size(), 512, [&](std::size_t begin, std::size_t end) {
    const std::vector<UniformAttribute> *previous_fields = nullptr;
    std::shared_ptr<const AttributeLayout> previous_layout;
    for (std::size_t index = begin; index < end; ++index) {
      auto &placement = placed[index];
      if (!components.contains(placement.entry->second.geometry_key))
        continue;
      placement.root = &prepared->origins->at(placement.entry->first).root;
      const auto &fields = placement.entry->second.attributes;
      if (!previous_fields || !same_fields(*previous_fields, fields)) {
        previous_layout = std::make_shared<const AttributeLayout>(attribute_layout(fields));
        previous_fields = &fields;
      }
      placement.layout = previous_layout;
    }
  });
  struct Combination {
    const std::string *root{};
    std::string original, variant;
    const AttributeLayout *layout{};
    std::uint64_t *count{};
    // Records are never erased during this walk, so found entries stay valid.
    std::map<std::string, Record>::iterator source, target;
  };
  std::vector<Combination> combinations;
  // Combinations are unique in (root, original key, layout), so the first
  // match is the only match: try the previous object's combination first.
  // Objects take their variant key after the walk (nothing in the walk reads
  // another object's key), in parallel.
  std::size_t previous_combination = std::numeric_limits<std::size_t>::max();
  for (auto &placement : placed) {
    if (!placement.root)
      continue;
    const auto &key = placement.entry->first;
    auto &object = placement.entry->second;
    const auto &layout = *placement.layout;
    const auto matches = [&](const Combination &value) {
      return (value.root == placement.root || *value.root == *placement.root) &&
             value.original == object.geometry_key && (value.layout == &layout || *value.layout == layout);
    };
    auto combination = previous_combination < combinations.size() && matches(combinations[previous_combination])
                           ? combinations.begin() + std::ptrdiff_t(previous_combination)
                           : std::find_if(combinations.begin(), combinations.end(), matches);
    bool first = false;
    if (combination == combinations.end()) {
      first = true;
      Combination value{placement.root, object.geometry_key, variant_key(object.geometry_key, layout), &layout};
      value.source = value.target = cache_.end();
      value.count = &populations[{*placement.root, value.original}][layout];
      combinations.push_back(std::move(value));
      combination = std::prev(combinations.end());
    }
    previous_combination = std::size_t(combination - combinations.begin());
    placement.combination = previous_combination;
    ++*combination->count;
    const auto &original = combination->original;
    const auto &variant = combination->variant;
    if (first)
      variants[original].emplace(layout, variant);
    if (variant != original) {
      if (first)
        prepared->instance_sample_sources.emplace(variant, original);
      // The same lookups as before, served from the entries found earlier.
      if (combination->source == cache_.end())
        combination->source = cache_.find(original);
      if (combination->target == cache_.end())
        combination->target = cache_.find(variant);
      const auto source = combination->source;
      auto &target = combination->target;
      const bool stale = source != cache_.end() &&
                         (target == cache_.end() || target->second.identifiers != source->second.identifiers ||
                          target->second.material_key != source->second.material_key);
      if (source != cache_.end()) {
        if (target == cache_.end())
          target = cache_.try_emplace(variant).first;
        target->second.component_signature = source->second.component_signature;
      }
      if (stale && !sampled.contains(original))
        if (const auto task = prepared->packed_deformations.find(key); task != prepared->packed_deformations.end()) {
          const auto &input = task->second;
          std::vector<NativeDeformationSample> inputs;
          inputs.reserve(input.samples.size());
          for (const auto &sample : input.samples)
            inputs.push_back(sample ? sample->input : NativeDeformationSample{});
          PreparedScene::Deformation data;
          data.data = prepare_deformation_samples(input.kind, input.nominal->input, std::move(inputs),
                                                  source->second.identifiers, *input.window, input.warning);
          data.keys.emplace(input.kind, original);
          prepared->deformations.push_back(std::move(data));
          sampled.insert(original);
        }
    }
  }
  parallel_slots(placed.size(), 512, [&](std::size_t begin, std::size_t end) {
    for (std::size_t index = begin; index < end; ++index)
      if (placed[index].root)
        placed[index].entry->second.geometry_key = combinations[placed[index].combination].variant;
  });
  phase("convert_attributes_layouts_walk");
  std::vector<PreparedScene::Component> resolved;
  std::set<std::pair<std::string, std::string>> emitted;
  for (const auto &component : prepared->components) {
    const auto found = populations.find({component.root, component.key});
    if (!component.multiplicity || found == populations.end()) {
      resolved.push_back(component);
      continue;
    }
    for (const auto &[layout, count] : found->second) {
      const auto key = variant_key(component.key, layout);
      if (!emitted.emplace(component.root, key).second)
        continue;
      auto data = component;
      data.key = key;
      data.request.instance_attributes = layout;
      data.multiplicity = count;
      resolved.push_back(std::move(data));
    }
  }
  prepared->components = std::move(resolved);
  for (const auto &[original, layouts] : variants) {
    const auto details = desired.geometry_details.at(original);
    desired.geometry.erase(original);
    for (const auto &[layout, key] : layouts) {
      desired.geometry.insert(key);
      desired.geometry_details[key] = details;
    }
    if (std::none_of(layouts.begin(), layouts.end(), [&](const auto &entry) { return entry.second == original; }))
      desired.geometry_details.erase(original);
  }
  for (auto &[alias, keys] : desired.geometry_aliases) {
    std::set<std::string> expanded;
    for (const auto &key : keys)
      if (const auto found = variants.find(key); found != variants.end()) {
        for (const auto &[layout, target] : found->second)
          expanded.insert(target);
      } else
        expanded.insert(key);
    keys = std::move(expanded);
  }
  for (const auto &root : roots)
    if (auto found = desired.packed_deformations.find(root); found != desired.packed_deformations.end()) {
      auto &state = found->second;
      state.components.clear();
      for (const auto &component : prepared->components)
        if (component.root == root && component.multiplicity)
          state.components.push_back({component.selection->kind, component.key, component.base_key,
                                      component.request.sop_path, component.request.shader_attributes,
                                      component.multiplicity, component.request.pointiness});
    }
  phase("convert_attributes_layouts");
  for (auto &[key, failure] : failures) {
    failure.issue.message += " (" + std::to_string(failure.count) + " affected components)";
    failure.issue.resource = desired.objects.at(failure.issue.resource).geometry_key;
    prepared->diagnostics.insert_or_assign(
        {failure.issue.code + ":" + failure.issue.attribute + ":" + std::get<3>(key), failure.issue.source},
        std::move(failure.issue));
  }
  prepared->instance_sources.clear(); // No source pointer enters a snapshot or worker message.
  prepared->recycled_sources.clear();
  prepared->instance_field_shutters.clear();
}
SceneUpdate NativeSceneAdapter::convert(const std::shared_ptr<PreparedScene> &prepared) {
  if (!prepared || prepared->owner != this || prepared->serial != preparation_serial_ || prepared->consumed)
    throw std::logic_error("Native scene preparation is stale, consumed or belongs to another adapter");
  if (pending_)
    throw std::logic_error("A converted native scene must be accepted or discarded before another conversion");
  prepared->consumed = true;
  const auto *convert_profile_value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY");
  const bool convert_profiling = convert_profile_value && std::strcmp(convert_profile_value, "1") == 0;
  auto convert_phase_started = Clock::now();
  const auto convert_phase = [&](const char *name) {
    if (convert_profiling) {
      stats_.packed_assembly_seconds[name] += elapsed(convert_phase_started);
      convert_phase_started = Clock::now();
    }
  };
  release_retired();
  // Acquired requests retain their immutable result tables. Retire the
  // continuation's parsing dictionaries/slots under converter ownership,
  // rather than freeing potentially large source tables on the UI at commit.
  style_cache_.release_capture_work();
  style_binding_cache_.clear();
  if (material_motion_cache_)
    material_motion_cache_->release_capture_work();
  if (packed_preparation_cache_)
    packed_preparation_cache_->clear();
  if (packed_grouping_cache_)
    packed_grouping_cache_->clear();
  convert_phase("convert_release");
  if (prepared->authoritative)
    assemble_packed(prepared);
  convert_phase("convert_assemble_packed");
  SceneUpdate update;
  if (prepared->authoritative)
    update.source_diagnostics.emplace();
  if (!prepared->authoritative)
    return update;
  auto &desired = prepared->desired;
  for (const auto &[key, external] : prepared->external_meshes) {
    auto &record = cache_[key];
    if (record.external_mesh != external.source || !active_.geometry.contains(key)) {
      record.revision = external.mesh->revision;
      record.external_mesh = external.source;
      record.external_render_mesh = external.mesh;
      // Link hair and point clouds: the carrier's curves or points are the engine geometry under
      // this key.
      if (external.mesh->external_curves)
        update.curves.push_back(external.mesh->external_curves);
      else if (external.mesh->external_points)
        update.points.push_back(external.mesh->external_points);
      else
        update.meshes.push_back(external.mesh);
      ++stats_.extractions;
    } else
      ++stats_.cache_hits;
    record.material_references = external.references;
  }
  // Certify only the complete, currently accepted direct population. Material
  // resolution above is always current; a source/layout/binding/model/pose edit
  // takes normal expansion. There is no previous-frame population history.
  if (prepared->packed_population_reuse) {
    // Host preparation already re-resolved every unique binding and verified
    // identical source/rendered mesh owners. Only current provenance changes;
    // the accepted instance/alias maps and navigation origins remain immutable.
    if (!update.meshes.empty() || !update.curves.empty() || !update.points.empty())
      throw std::logic_error("Certified packed population unexpectedly changed geometry");
    for (const auto &key : desired.geometry)
      desired.native_materials.emplace(key, cache_.at(key).material_references);
    stats_.packed_instances = prepared->packed_instances;
    stats_.index_instance_ids = prepared->index_ids;
    stats_.points = stats_.curves = stats_.index_primitive_ids = 0;
    stats_.motion_warnings.clear();
    ++stats_.packed_population_reuses;
    pending_ = std::move(desired);
    pending_population_reuse_ = true;
    pending_ready_ = false;
    return update;
  }
  // Attribute instancing also supplies an ordinary source request with points
  // disabled. Its empty navigation root is current metadata, not a second
  // contributing population; the actual geometry/recipe checks below prove
  // that this one direct batch owns every rendered object.
  if (prepared->direct_instances.size() == 1 && desired.objects.empty() && prepared->components.empty() &&
      prepared->packed_leaves.empty() && prepared->packed_populations.empty() &&
      prepared->retained_packed_roots.empty() && prepared->volumes.empty() && prepared->deformations.empty() &&
      prepared->motion_assemblies.empty() && desired.packed_deformations.empty() && desired.instance_fields.empty() &&
      desired.unavailable_geometry.empty()) {
    const auto &batch = prepared->direct_instances.front();
    DirectPopulationState population{batch.sample,    batch.bindings, batch.root, batch.geometry_alias,
                                     batch.transform, batch.model,    {}};
    for (const auto &[key, external] : prepared->external_meshes)
      population.meshes.emplace(key, std::make_pair(external.source, external.mesh));
    desired.direct_population = std::move(population);
    if (active_.direct_population == desired.direct_population && update.meshes.empty() && update.curves.empty() &&
        update.points.empty() && active_.geometry == desired.geometry &&
        active_.objects.size() == batch.sample->direct.size() && active_.navigation && active_.navigation->origins &&
        active_.instance_fields.empty() && active_.packed_deformations.empty() &&
        active_.unavailable_geometry.empty()) {
      // Keep accepted maps immutable until commit. The owned snapshot can share
      // its object table while navigation advances to this capture's time.
      desired.navigation->origins = active_.navigation->origins;
      for (const auto &key : desired.geometry)
        desired.native_materials.emplace(key, cache_.at(key).material_references);
      for (const auto &[key, issue] : prepared->diagnostics)
        update.source_diagnostics->push_back(issue);
      stats_.packed_instances = batch.sample->direct.size();
      stats_.index_instance_ids = stats_.points = stats_.curves = stats_.index_primitive_ids = 0;
      stats_.motion_warnings.clear();
      ++stats_.direct_population_reuses;
      pending_ = std::move(desired);
      pending_population_reuse_ = true;
      pending_ready_ = false;
      return update;
    }
  }
  const auto direct_started = Clock::now();
  std::set<std::pair<std::string, std::string>> blender_placements; // apply_blender_object
  for (const auto &batch : prepared->direct_instances) {
    // Packed deformation assembly can rebuild a shared geometry alias before
    // these deferred objects exist. Restore this recipe's resolved bindings.
    auto &geometry_aliases = desired.geometry_aliases[batch.geometry_alias];
    geometry_aliases.insert(batch.bindings.begin(), batch.bindings.end());
    auto &aliases = desired.object_aliases[batch.root];
    for (const auto &placement : batch.sample->direct) {
      auto data = batch.model;
      data.key = batch.root + "/external:" + placement.identity;
      data.geometry_key = batch.bindings.at(placement.binding_index);
      data.transform = multiply(batch.transform, placement.transform);
      data.random_id = random_id(data.key);
      data.attributes = placement.attributes;
      apply_blender_object(data, *placement.request,
                           placement.request->blender_object &&
                               blender_placements.emplace(batch.root, placement.request->object_key).second);
      if (!data.properties.cryptomatte_object.empty())
        data.properties.cryptomatte_object += "/external:" + placement.identity;
      prepared->origins->emplace(data.key, NativeNavigationState::Origin{batch.root, placement.transform, {}});
      aliases.insert(data.key);
      const auto [found, inserted] = desired.objects.try_emplace(data.key, std::move(data));
      if (!inserted)
        throw std::invalid_argument("Duplicate native object identity: " + found->first);
      ++prepared->packed_instances;
      ++stats_.direct_instances_expanded;
    }
  }
  if (const auto *value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"); value && std::strcmp(value, "1") == 0)
    stats_.packed_assembly_seconds["conversion_direct_instances"] += elapsed(direct_started);
  convert_phase("convert_direct");
  // Packed leaves. Each placement's records depend only on that placement
  // and immutable batch state, so they are written in parallel into their own
  // slots. The full path then publishes new records serially in key order
  // (the tables are ordered maps and sets, so the result equals placement-order
  // insertion). When the retired generation's tables hold exactly the keys this
  // capture would build, the recycled path writes the same values into those
  // nodes instead (PackedLayout; docs/dev/notes/native-scene-publication.md).
  const auto *full_conversion_value = std::getenv("CYCLES_HOUDINI_FULL_PACKED_CONVERSION");
  const bool full_conversion = full_conversion_value && std::strcmp(full_conversion_value, "1") == 0;
  std::vector<const PreparedScene::PackedLeaves *> leaf_batches;
  for (const auto &batch : prepared->packed_leaves)
    if (!batch.sampled)
      leaf_batches.push_back(&batch);
  struct LeafSlot {
    const NativePackedInstance *placement{};
    std::size_t group{}, first{};
  };
  const auto leaf_slots = [](const PreparedScene::PackedLeaves &batch, std::size_t &total) {
    std::vector<LeafSlot> slots;
    total = 0;
    batch.each_placement([&](const NativePackedInstance &placement, std::size_t group_index) {
      slots.push_back({&placement, group_index, total});
      total += batch.groups.at(group_index).objects.size();
    });
    return slots;
  };
  using InstanceSourcePointer = std::shared_ptr<const PreparedScene::InstanceSource>;
  // Everything one placement contributes. `output(model_index)` returns the
  // object, origin and attribute-source slots to write: new records on the
  // full path, recycled nodes otherwise. One code path gives both the same values.
  const auto write_placement = [&](const PreparedScene::PackedLeaves &batch, const NativePackedInstance &placement,
                                   const PreparedScene::PackedLeaves::Group &group,
                                   const NativeNavigationState::Root *root, char &invalid_velocity,
                                   const auto &output) {
    const auto prefix = batch.prefix + "/packed:" + placement.identity;
    auto transform = multiply(batch.transform, placement.transform);
    const auto local = multiply(batch.local_transform, placement.transform);
    std::vector<Matrix> local_motion, object_motion;
    std::optional<Matrix> nominal_transform;
    if (batch.velocity) {
      bool invalid = false;
      local_motion =
          packed_velocity_samples(batch.local_transform, batch.local_motion.empty() ? nullptr : &batch.local_motion,
                                  placement.transform, placement.velocity, batch.window, root->motion, invalid,
                                  placement.angular_velocity, placement.pivot, placement.acceleration);
      invalid_velocity = invalid;
      if (local_motion.empty())
        local_motion.assign(batch.window.times.size(), local);
      transform = multiply(root->centre, local_motion.at(local_motion.size() / 2));
      object_motion.reserve(local_motion.size());
      for (std::size_t i = 0; i < local_motion.size(); ++i)
        object_motion.push_back(multiply(root->motion.at(i), local_motion[i]));
      if (std::all_of(object_motion.begin(), object_motion.end(),
                      [&](const auto &matrix) { return matrix == transform; }))
        object_motion.clear();
      if (batch.window.centre != batch.window.nominal)
        nominal_transform = multiply(root->nominal, local);
    }
    for (std::size_t model_index = 0; model_index < group.objects.size(); ++model_index) {
      const auto &model = group.objects[model_index];
      auto [data, origin, source] = output(model_index);
      data = model;
      data.key = prefix + model.key;
      data.transform = transform;
      data.random_id = random_id(data.key);
      if (group.instance_attributes)
        source = std::make_shared<PreparedScene::InstanceSource>(PreparedScene::InstanceSource{
            batch.source,
            placement.primitive_offset,
            batch.source_path,
            batch.ancestors,
            {std::size_t(&placement - batch.rows->values.data()), placement.identity, placement.index_identity},
            batch.rows->values.size()});
      data.motion = object_motion;
      data.nominal_transform = nominal_transform;
      origin = NativeNavigationState::Origin{batch.object_alias, local, local_motion};
      if (!data.properties.cryptomatte_object.empty())
        data.properties.cryptomatte_object += "/packed:" + placement.identity;
    }
  };
  // Diagnostics keep placement order: the first invalid instance names the issue.
  const auto report_invalid_velocity = [&](const PreparedScene::PackedLeaves &batch, const std::vector<LeafSlot> &slots,
                                           const std::vector<char> &invalid_velocity) {
    for (std::size_t index = 0; index < slots.size(); ++index)
      if (invalid_velocity[index]) {
        auto [found, inserted] = prepared->invalid_packed_velocity.try_emplace(
            batch.source_path, 0,
            SourceDiagnostic{
                "motion.packed_velocity", batch.source_path,
                batch.prefix + "/packed:" + slots[index].placement->identity,
                "Packed velocity produces an unrepresentable shutter transform (non-finite, or w turns half a "
                "turn or more between two motion steps; raise Motion Samples)",
                "Rendering this instance without its packed velocity; retaining independent parent, OBJ "
                "and prototype motion",
                "warning", "v"});
        ++found->second.first;
      }
  };
  const auto leaf_root = [&](const PreparedScene::PackedLeaves &batch) {
    return batch.velocity ? &desired.navigation->roots.at(batch.object_alias) : nullptr;
  };
  // Recycling (PackedLayout): the retired generation's object table, origin
  // map and leaf alias sets are kept instead of freed. This capture's keys are
  // merged into them: surviving nodes are overwritten in place, dead nodes
  // erased, born keys inserted, so the tables end with exactly this capture's
  // keys and values. Direct instances and retained packed roots insert into
  // these tables after the leaves; captures with them neither record nor recycle.
  std::shared_ptr<PackedLayout> layout;
  bool recycled = false;
  const bool recyclable = !full_conversion && prepared->direct_instances.empty() &&
                          prepared->retained_packed_roots.empty() && desired.navigation &&
                          desired.navigation->origins == prepared->origins;
  std::vector<const PackedLayout::ObjectEntry *> recycled_order;
  if (!recyclable)
    stats_.packed_recycle_fallback = full_conversion ? "disabled" : "structure";
  else if (!spare_)
    stats_.packed_recycle_fallback = "no_spare";
  else {
    using ObjectEntry = PackedLayout::ObjectEntry;
    using OriginEntry = PackedLayout::OriginEntry;
    // Read-only planning: every check and every key before anything is written.
    struct Plan {
      std::vector<std::vector<LeafSlot>> slots;
      std::vector<std::size_t> batch_first; // Leaf index of each batch's first slot object.
      std::vector<std::string> keys;        // Leaf keys, leaf index order.
      std::vector<std::size_t> order;       // Leaf indices in key order.
      std::vector<std::size_t> key_aliases; // Alias bucket of each leaf, leaf index order.
      // Every object key of this capture in key order: a fresh entry or a leaf index.
      std::vector<std::pair<std::pair<const std::string, ObjectData> *, std::size_t>> objects;
      std::vector<std::pair<OriginEntry *, std::size_t>> origins;
      std::vector<std::pair<std::string, std::vector<const std::string *>>> aliases;
    } plan;
    constexpr auto leaf = std::numeric_limits<std::size_t>::max();
    const auto make_plan = [&]() -> const char * {
      const auto &spare = *spare_;
      const auto &previous = *spare.layout;
      if (spare.objects.identity() != previous.table || spare.objects.revision() != previous.revision ||
          !spare.navigation || spare.navigation->origins.get() != previous.origins)
        return "edited";
      // Only this adapter creates owners of these tables, and the retired
      // generation is no longer reachable through it: a count of one cannot
      // rise concurrently. The fence orders the writes below after the last
      // release (and so the last read) by a former owner on another thread.
      if (!spare.objects.exclusive() || spare.navigation.use_count() != 1 || spare.navigation->origins.use_count() != 1)
        return "shared";
      std::atomic_thread_fence(std::memory_order_acquire);
      std::size_t total = 0;
      std::map<std::string, std::size_t> alias_indices;
      std::vector<std::size_t> batch_aliases;
      plan.slots.resize(leaf_batches.size());
      for (std::size_t index = 0; index < leaf_batches.size(); ++index) {
        const auto &alias = leaf_batches[index]->object_alias;
        const auto [found, inserted] = alias_indices.try_emplace(alias, plan.aliases.size());
        if (inserted)
          plan.aliases.emplace_back(alias, std::vector<const std::string *>{});
        batch_aliases.push_back(found->second);
        std::size_t count = 0;
        plan.slots[index] = leaf_slots(*leaf_batches[index], count);
        plan.batch_first.push_back(total);
        total += count;
      }
      plan.keys.resize(total);
      plan.key_aliases.resize(total);
      for (std::size_t index = 0; index < leaf_batches.size(); ++index) {
        const auto &batch = *leaf_batches[index];
        const auto &slots = plan.slots[index];
        const auto base = plan.batch_first[index];
        parallel_slots(slots.size(), 256, [&](std::size_t begin, std::size_t end) {
          for (std::size_t slot = begin; slot < end; ++slot) {
            const auto prefix = batch.prefix + "/packed:" + slots[slot].placement->identity;
            const auto &group = batch.groups.at(slots[slot].group);
            for (std::size_t model_index = 0; model_index < group.objects.size(); ++model_index) {
              const auto leaf_index = base + slots[slot].first + model_index;
              plan.keys[leaf_index] = prefix + group.objects[model_index].key;
              plan.key_aliases[leaf_index] = batch_aliases[index];
            }
          }
        });
      }
      plan.order.resize(total);
      std::iota(plan.order.begin(), plan.order.end(), std::size_t{0});
      parallel_sort(plan.order, [&](std::size_t a, std::size_t b) { return plan.keys[a] < plan.keys[b]; });
      // Any duplicate makes the full path reject this capture with its own message.
      for (std::size_t n = 1; n < plan.order.size(); ++n)
        if (plan.keys[plan.order[n - 1]] == plan.keys[plan.order[n]])
          return "duplicate";
      // Equal keys in the prepared entries and the leaves: a duplicate object
      // (rejected by the full path) or a leaf origin the full path would drop.
      const auto merge = [&](auto &fresh, auto &target) -> bool {
        target.reserve(fresh.size() + plan.order.size());
        auto entry = fresh.begin();
        for (const auto index : plan.order) {
          const auto &key = plan.keys[index];
          for (; entry != fresh.end() && entry->first < key; ++entry)
            target.emplace_back(&*entry, leaf);
          if (entry != fresh.end() && entry->first == key)
            return false;
          target.emplace_back(nullptr, index);
        }
        for (; entry != fresh.end(); ++entry)
          target.emplace_back(&*entry, leaf);
        return true;
      };
      if (!merge(desired.objects.edit(), plan.objects))
        return "duplicate";
      if (!merge(*prepared->origins, plan.origins))
        return "structure";
      if (!prepared->instance_sources.empty())
        for (const auto index : plan.order)
          if (prepared->instance_sources.contains(plan.keys[index]))
            return "structure";
      // Route each sorted leaf once. Each bucket inherits global key order;
      // ownership comes from its batch, including repeated aliases and empty
      // batches, without rescanning all leaves for every source alias.
      for (const auto index : plan.order)
        plan.aliases[plan.key_aliases[index]].second.push_back(&plan.keys[index]);
      // Alias sets: keys before the leaves plus every leaf key of that alias.
      for (auto &[alias, keys] : plan.aliases) {
        const auto prior = desired.object_aliases.find(alias);
        if (prior == desired.object_aliases.end())
          continue;
        auto leaves = std::move(keys);
        keys.reserve(prior->second.size() + leaves.size());
        auto entry = prior->second.begin();
        for (const auto *key : leaves) {
          for (; entry != prior->second.end() && *entry < *key; ++entry)
            keys.push_back(&*entry);
          if (entry != prior->second.end() && *entry == *key)
            ++entry; // A set holds one copy.
          keys.push_back(key);
        }
        for (; entry != prior->second.end(); ++entry)
          keys.push_back(&*entry);
      }
      return nullptr;
    };
    if (const auto *reason = make_plan()) {
      stats_.packed_recycle_fallback = reason;
      spare_.reset();
    } else {
      // Consumed before the first write: an exception below drops these
      // tables, so no later capture can accept a half-written generation.
      const auto taken = std::move(spare_);
      layout = std::move(taken->layout);
      layout->aliases.clear();
      auto &objects = taken->objects.edit(); // Exclusive: no copy; bumps the revision.
      auto &origins = *std::const_pointer_cast<NativeNavigationState::Origins>(taken->navigation->origins);
      const auto total = plan.keys.size();
      std::vector<ObjectEntry *> object_nodes(total);
      std::vector<OriginEntry *> origin_nodes(total);
      std::vector<const ObjectEntry *> ordered(plan.objects.size());
      std::uint64_t reused = 0;
      // Merge a planned key sequence into a recycled ordered container: keep
      // equal keys, erase keys that are no longer present, insert new ones.
      const auto merge_into = [](auto &target, std::size_t count, const auto &key_of, const auto &found) {
        std::uint64_t kept = 0;
        auto node = target.begin();
        for (std::size_t position = 0; position < count; ++position) {
          const std::string &key = key_of(position);
          while (node != target.end() && node->first < key)
            node = target.erase(node);
          if (node != target.end() && node->first == key) {
            found(position, *node);
            ++node;
            ++kept;
          } else
            found(position, *target.emplace_hint(node, std::piecewise_construct, std::forward_as_tuple(key),
                                                 std::forward_as_tuple()));
        }
        target.erase(node, target.end());
        return kept;
      };
      std::vector<std::pair<std::string, std::set<std::string>>> alias_sets(plan.aliases.size());
      const auto merge_objects = [&] {
        reused = merge_into(
            objects, plan.objects.size(),
            [&](std::size_t position) -> const std::string & {
              const auto &[fresh, index] = plan.objects[position];
              return fresh ? fresh->first : plan.keys[index];
            },
            [&](std::size_t position, ObjectEntry &node) {
              const auto &[fresh, index] = plan.objects[position];
              if (fresh)
                node.second = std::move(fresh->second);
              else
                object_nodes[index] = &node;
              ordered[position] = &node;
            });
      };
      const auto merge_origins = [&] {
        merge_into(
            origins, plan.origins.size(),
            [&](std::size_t position) -> const std::string & {
              const auto &[fresh, index] = plan.origins[position];
              return fresh ? fresh->first : plan.keys[index];
            },
            [&](std::size_t position, OriginEntry &node) {
              const auto &[fresh, index] = plan.origins[position];
              if (fresh)
                node.second = std::move(fresh->second);
              else
                origin_nodes[index] = &node;
            });
      };
      const auto merge_aliases = [&] {
        for (std::size_t index = 0; index < plan.aliases.size(); ++index) {
          const auto &[alias, keys] = plan.aliases[index];
          auto &[name, target] = alias_sets[index];
          name = alias;
          if (const auto found = taken->aliases.find(alias); found != taken->aliases.end())
            target = std::move(found->second);
          auto node = target.begin();
          for (const auto *key : keys) {
            while (node != target.end() && *node < *key)
              node = target.erase(node);
            if (node != target.end() && *node == *key)
              ++node;
            else
              target.emplace_hint(node, *key);
          }
          target.erase(node, target.end());
        }
      };
      // Three independent containers: merged concurrently, each serially in key order.
      if (serial_conversion()) {
        merge_objects();
        merge_origins();
        merge_aliases();
      } else
        UTparallelInvoke(true, merge_objects, merge_origins, merge_aliases);
      convert_phase("convert_leaves_recycle_merge");
      std::vector<InstanceSourcePointer> leaf_sources(total);
      for (std::size_t index = 0; index < leaf_batches.size(); ++index) {
        const auto &batch = *leaf_batches[index];
        for (const auto &group : batch.groups)
          for (const auto &model : group.objects)
            desired.geometry_aliases[batch.geometry_alias].insert(model.geometry_key);
        const auto &slots = plan.slots[index];
        const auto base = plan.batch_first[index];
        std::vector<char> invalid_velocity(slots.size());
        const auto *root = leaf_root(batch);
        parallel_slots(slots.size(), 256, [&](std::size_t begin, std::size_t end) {
          for (std::size_t slot = begin; slot < end; ++slot) {
            const auto first = base + slots[slot].first;
            write_placement(batch, *slots[slot].placement, batch.groups.at(slots[slot].group), root,
                            invalid_velocity[slot], [&](std::size_t model_index) {
                              const auto position = first + model_index;
                              return std::tie(object_nodes[position]->second, origin_nodes[position]->second,
                                              leaf_sources[position]);
                            });
          }
        });
        report_invalid_velocity(batch, slots, invalid_velocity);
        const auto next = index + 1 < leaf_batches.size() ? plan.batch_first[index + 1] : total;
        prepared->packed_instances += next - base;
      }
      convert_phase("convert_leaves_recycle_write");
      // assemble_instance_attributes walks the sources in key order.
      auto source = prepared->instance_sources.begin();
      for (const auto index : plan.order) {
        if (!leaf_sources[index])
          continue;
        for (; source != prepared->instance_sources.end() && source->first < plan.keys[index]; ++source)
          prepared->recycled_sources.emplace_back(&source->first, source->second);
        prepared->recycled_sources.emplace_back(&object_nodes[index]->first, std::move(leaf_sources[index]));
      }
      for (; source != prepared->instance_sources.end(); ++source)
        prepared->recycled_sources.emplace_back(&source->first, source->second);
      desired.objects = std::move(taken->objects);
      const auto recycled_origins = std::const_pointer_cast<NativeNavigationState::Origins>(taken->navigation->origins);
      prepared->origins = recycled_origins;
      desired.navigation->origins = recycled_origins;
      for (auto &[alias, keys] : alias_sets) {
        desired.object_aliases[alias] = std::move(keys);
        layout->aliases.push_back(alias);
      }
      recycled_order = std::move(ordered);
      prepared->recycled = true;
      recycled = true;
      stats_.packed_recycled_objects = reused;
      convert_phase("convert_leaves_recycle_sources");
    }
  }
  if (recyclable && !recycled) {
    layout = std::make_shared<PackedLayout>();
    for (const auto *batch : leaf_batches)
      if (std::find(layout->aliases.begin(), layout->aliases.end(), batch->object_alias) == layout->aliases.end())
        layout->aliases.push_back(batch->object_alias);
  }
  for (std::size_t batch_index = 0; !recycled && batch_index < leaf_batches.size(); ++batch_index) {
    const auto &batch = *leaf_batches[batch_index];
    for (const auto &group : batch.groups)
      for (const auto &model : group.objects)
        desired.geometry_aliases[batch.geometry_alias].insert(model.geometry_key);
    std::size_t total = 0;
    const auto slots = leaf_slots(batch, total);
    struct Built {
      ObjectData data;
      std::string object_key, origin_key, alias_key, source_key;
      NativeNavigationState::Origin origin;
      InstanceSourcePointer source;
    };
    std::vector<Built> built(total);
    std::vector<char> invalid_velocity(slots.size());
    const auto *root = leaf_root(batch);
    parallel_slots(slots.size(), 256, [&](std::size_t begin, std::size_t end) {
      for (std::size_t index = begin; index < end; ++index) {
        const auto first = slots[index].first;
        write_placement(batch, *slots[index].placement, batch.groups.at(slots[index].group), root,
                        invalid_velocity[index], [&](std::size_t model_index) {
                          auto &output = built[first + model_index];
                          return std::tie(output.data, output.origin, output.source);
                        });
        for (auto position = first; position < first + batch.groups.at(slots[index].group).objects.size(); ++position) {
          auto &output = built[position];
          output.object_key = output.origin_key = output.alias_key = output.data.key;
          if (output.source)
            output.source_key = output.data.key;
        }
      }
    });
    convert_phase("convert_leaves_build");
    report_invalid_velocity(batch, slots, invalid_velocity);
    std::vector<std::size_t> order(total);
    std::iota(order.begin(), order.end(), std::size_t{0});
    parallel_sort(order, [&](std::size_t a, std::size_t b) { return built[a].data.key < built[b].data.key; });
    convert_phase("convert_leaves_sort");
    for (std::size_t n = 1; n < order.size(); ++n)
      if (built[order[n - 1]].data.key == built[order[n]].data.key)
        throw std::invalid_argument("Duplicate native object identity: " + built[order[n]].data.key);
    auto &aliases = desired.object_aliases[batch.object_alias];
    auto &origins = *prepared->origins;
    auto &objects = desired.objects.edit();
    auto object_hint = objects.end();
    auto origin_hint = origins.end();
    auto alias_hint = aliases.end();
    auto source_hint = prepared->instance_sources.end();
    if (!order.empty()) {
      const auto &first = built[order.front()].data.key;
      object_hint = objects.lower_bound(first);
      origin_hint = origins.lower_bound(first);
      alias_hint = aliases.lower_bound(first);
      source_hint = prepared->instance_sources.lower_bound(first);
    }
    for (const auto index : order) {
      auto &output = built[index];
      const auto before = objects.size();
      object_hint = std::next(objects.emplace_hint(object_hint, std::move(output.object_key), std::move(output.data)));
      if (objects.size() == before)
        throw std::invalid_argument("Duplicate native object identity: " + std::prev(object_hint)->first);
      origin_hint =
          std::next(origins.emplace_hint(origin_hint, std::move(output.origin_key), std::move(output.origin)));
      alias_hint = std::next(aliases.emplace_hint(alias_hint, std::move(output.alias_key)));
      if (output.source)
        source_hint = std::next(prepared->instance_sources.emplace_hint(source_hint, std::move(output.source_key),
                                                                        std::move(output.source)));
    }
    prepared->packed_instances += total;
    convert_phase("convert_leaves_insert");
    built.clear();
    convert_phase("convert_leaves_free");
  }
  convert_phase("convert_packed_leaves");
  assemble_instance_attributes(prepared);
  convert_phase("convert_instance_attributes");
  for (const auto &[key, issue] : prepared->diagnostics)
    update.source_diagnostics->push_back(issue);
  // All entries below are committed owned values. Never cook or dereference
  // an OP/GU node here. An authoritative update still rebuilds its complete
  // object/alias set, so deletions and other roots retain normal semantics.
  for (const auto &root_key : prepared->retained_packed_roots) {
    const auto &state = active_.packed_deformations.at(root_key);
    const auto &root = desired.navigation->roots.at(root_key);
    desired.packed_deformations.emplace(root_key, state);
    prepared->packed_instances += state.packed_instances;
    prepared->index_ids += state.index_ids;
    update.source_diagnostics->insert(update.source_diagnostics->end(), state.diagnostics.begin(),
                                      state.diagnostics.end());
    for (const auto &key : active_.object_aliases.at(root_key)) {
      auto data = active_.objects.at(key);
      const auto &origin = active_.navigation->origins->at(key);
      data.motion.clear();
      data.nominal_transform.reset();
      data.transform =
          multiply(root.centre, origin.motion.empty() ? origin.local : origin.motion.at(root.motion.size() / 2));
      for (std::size_t step = 0; step < root.motion.size(); ++step)
        data.motion.push_back(
            multiply(root.motion[step], origin.motion.empty() ? origin.local : origin.motion.at(step)));
      if (std::all_of(data.motion.begin(), data.motion.end(),
                      [&](const auto &matrix) { return matrix == data.transform; }))
        data.motion.clear();
      if (root.window.centre != root.window.nominal)
        data.nominal_transform = multiply(root.nominal, origin.local);
      prepared->origins->emplace(key, origin);
      desired.geometry.insert(data.geometry_key);
      const auto &details = active_.geometry_details.at(data.geometry_key);
      desired.geometry_details[data.geometry_key].insert(details.begin(), details.end());
      desired.object_aliases[root_key].insert(key);
      desired.geometry_aliases[state.request.geometry_key].insert(data.geometry_key);
      if (!desired.objects.emplace(key, std::move(data)).second)
        throw std::logic_error("Retained packed object overlaps another source");
    }
    for (const auto &component : state.components) {
      const auto &details = active_.geometry_details.at(component.key);
      for (const auto detail : details) {
        if (active_.binding_details.contains(detail))
          desired.binding_details.insert(detail);
        if (active_.partition_details.contains(detail))
          desired.partition_details.insert(detail);
      }
    }
  }
  NativeMotionWindow window;
  std::uint64_t point_count = 0, curve_count = 0, primitive_index_ids = 0;
  std::set<std::string> captured;
  std::map<std::string, NativeDeformation> sampled_deformations;
  for (const auto &task : prepared->deformations) {
    auto data = convert_deformation_set(*task.data, ++next_revision_, stats_, true);
    for (const auto &[kind, key] : task.keys)
      sampled_deformations.emplace(key, std::move(data.at(kind)));
  }
  for (const auto &[key, source] : prepared->instance_sample_sources)
    if (const auto found = sampled_deformations.find(source); found != sampled_deformations.end())
      sampled_deformations.emplace(key, found->second);
  // Content-addressed packed prototypes (canonical_prototype) reach a retained
  // key with another detail's data ids: compare their whole-detail contents.
  const auto unchanged_component = [&](Record &record, const GU_Detail &source, const GeometryRequest &request,
                                       const NativeGeometrySelection &selection, const std::string &key) {
    const auto started = Clock::now();
    auto signature =
        component_signature(source, selection, request, window, key.find("packed:detail:") != std::string::npos);
    const auto duration = elapsed(started);
    stats_.component_check_seconds += duration;
    stats_.conversion_seconds += duration;
    if (signature)
      ++stats_.component_checks;
    const bool same = signature && record.revision && record.component_signature == signature &&
                      record.material_key == request.material_key;
    record.component_signature = std::move(signature);
    if (same)
      ++stats_.component_reuses;
    return same;
  };
  const auto deformation = [&](const GU_Detail &source, const GeometryRequest &request, const std::string &key,
                               bool count_points, bool count_curves) {
    desired.geometry.insert(key);
    auto &record = cache_[key];
    if (captured.insert(key).second && sampled_deformations.contains(key)) {
      auto &sampled = sampled_deformations.at(key);
      std::visit(
          [&](auto &data) {
            data.key = key;
            data.instance_attributes = request.instance_attributes;
            record.revision = data.revision;
            using Data = std::decay_t<decltype(data)>;
            if constexpr (std::is_same_v<Data, MeshData>)
              update.meshes.push_back(std::make_shared<MeshData>(std::move(data)));
            else if constexpr (std::is_same_v<Data, PointData>)
              update.points.push_back(std::make_shared<PointData>(std::move(data)));
            else
              update.curves.push_back(std::make_shared<CurveData>(std::move(data)));
          },
          sampled.data);
      record.identifiers = std::move(sampled.identifiers);
      record.material_key = request.material_key;
      record.material_references = std::move(sampled.material_references);
      record.shader_attributes = std::move(sampled.shader_attributes);
      record.motion_warning = std::move(sampled.motion_warning);
      record.source_diagnostics = std::move(sampled.source_diagnostics);
      record.primitive_count = sampled.primitive_count;
      record.index_ids = sampled.index_ids;
      ++stats_.extractions;
    } else
      ++stats_.cache_hits;
    if (count_points)
      point_count += record.primitive_count;
    if (count_curves)
      curve_count += record.primitive_count;
    primitive_index_ids += record.index_ids;
  };
  // Index only retained immutable full-detail recipes, once per conversion.
  // Data IDs and subdivision settings select candidates; mixed selections
  // keep their own provenance and never transplant another domain's offsets.
  std::optional<std::map<std::vector<std::int64_t>, std::shared_ptr<NativeMeshCaptureCache>>> mesh_recipes;
  const auto recipe_key = [](const NativeMeshCaptureCache &capture) {
    auto ids = capture.topology_ids;
    subdivision_identifiers(ids, capture.subdivision);
    return ids;
  };
  const auto mesh = [&](const GU_Detail &source, const GeometryRequest &request, const std::string &key,
                        const NativeGeometrySelection &selection) {
    if (window.enabled && request.motion == NativeGeometryMotion::Deformation) {
      deformation(source, request, key, false, false);
      return;
    }
    desired.geometry.insert(key);
    std::vector<std::int64_t> fingerprint = {id(source.getP()),
                                             source.getTopology().getDataId(),
                                             source.getPrimitiveList().getDataId(),
                                             source.getNumPoints(),
                                             source.getNumPrimitives(),
                                             id(source.findVertexAttribute("N")),
                                             id(source.findPointAttribute("N")),
                                             id(source.findVertexAttribute("uv")),
                                             id(source.findPointAttribute("uv")),
                                             request.smooth ? 1 : 0,
                                             id(source.findVertexAttribute("Cd")),
                                             id(source.findPointAttribute("Cd")),
                                             id(source.findPrimitiveAttribute("Cd")),
                                             id(source.findGlobalAttribute("Cd"))};
    subdivision_identifiers(fingerprint, request.subdivision);
    named_attribute_identifiers(fingerprint, source, request);
    if (request.subdivision.type != SubdivisionType::None) {
      if (request.subdivision.use_creases)
        for (const auto *attribute :
             {source.findVertexAttribute("creaseweight"), source.findPrimitiveAttribute("creaseweight"),
              source.findPointAttribute("cornerweight")})
          fingerprint.push_back(id(attribute));
      // Group and unsupported detail overrides also participate in edits.
      const auto *holes = source.findPrimitiveGroup("subdivision_hole");
      fingerprint.push_back(holes ? holes->entries() : 0);
      for (const auto *name : {"osd_scheme", "osd_vtxboundaryinterpolation", "osd_fvarlinearinterpolation",
                               "osd_creasingmethod", "osd_trianglesubdiv"})
        fingerprint.push_back(id(source.findGlobalAttribute(name)));
    }
    velocity_fingerprint(fingerprint, source, request, window);
    if (request.material_mode == NativeMaterialMode::Native) {
      for (const auto owner : {GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
        for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"})
          fingerprint.push_back(id(source.findAttribute(owner, name)));
    }
    fingerprint.push_back(static_cast<std::int64_t>(request.material_mode));
    auto &record = cache_[key];
    const bool valid_ids = fingerprint[0] != GA_INVALID_DATAID && fingerprint[1] != GA_INVALID_DATAID &&
                           fingerprint[2] != GA_INVALID_DATAID;
    if (captured.insert(key).second &&
        (!valid_ids || record.identifiers != fingerprint || record.material_key != request.material_key)) {
      if (unchanged_component(record, source, request, selection, key)) {
        record.identifiers = std::move(fingerprint);
        ++stats_.cache_hits;
        return;
      }
      const auto started = Clock::now();
      std::vector<Float3> velocities, accelerations;
      const bool velocity = usable_velocity(source, request, selection, window, record.motion_warning);
      const bool accelerated =
          velocity && acceleration_attribute(source, request, window, &record.motion_warning) != nullptr;
      record.material_references.clear();
      record.source_diagnostics.clear();
      NativeMeshCaptureMetrics mesh_metrics;
      if (velocity)
        record.mesh_capture.reset();
      if (!velocity && !record.mesh_capture && selection.full_detail &&
          source.getTopology().getDataId() != GA_INVALID_DATAID &&
          source.getPrimitiveList().getDataId() != GA_INVALID_DATAID) {
        if (!mesh_recipes) {
          mesh_recipes.emplace();
          for (const auto &[other_key, other] : cache_)
            if (other.mesh_capture && other.mesh_capture->full_detail && other.mesh_capture->data &&
                other.mesh_capture->topology_ids[0] != GA_INVALID_DATAID &&
                other.mesh_capture->topology_ids[1] != GA_INVALID_DATAID)
              mesh_recipes->try_emplace(recipe_key(*other.mesh_capture), other.mesh_capture);
        }
        std::vector<std::int64_t> ids = {source.getTopology().getDataId(), source.getPrimitiveList().getDataId(),
                                         source.getNumPoints(), source.getNumPrimitives(), source.getNumVertices()};
        subdivision_identifiers(ids, request.subdivision);
        if (const auto found = mesh_recipes->find(ids); found != mesh_recipes->end())
          record.mesh_capture = found->second;
      }
      auto data = std::make_shared<MeshData>(
          capture_mesh(source, request, ++next_revision_, velocity ? &velocities : nullptr, nullptr, true,
                       &record.material_references, &selection, velocity ? nullptr : &record.mesh_capture,
                       &mesh_metrics, &record.source_diagnostics, accelerated ? &accelerations : nullptr));
      data->key = key;
      data->instance_attributes = request.instance_attributes;
      if (record.mesh_capture)
        record.mesh_capture->data = data;
      if (mesh_recipes && record.mesh_capture && record.mesh_capture->full_detail)
        mesh_recipes->try_emplace(recipe_key(*record.mesh_capture), record.mesh_capture);
      stats_.mesh_topology_builds += mesh_metrics.topology_builds;
      stats_.mesh_topology_reuses += mesh_metrics.topology_reuses;
      stats_.mesh_topology_scans += mesh_metrics.topology_scans;
      stats_.mesh_triangulated_faces += mesh_metrics.triangulated_faces;
      stats_.mesh_retained_faces += mesh_metrics.retained_faces;
      stats_.mesh_named_fields_captured += mesh_metrics.named_fields_captured;
      stats_.mesh_named_fields_reused += mesh_metrics.named_fields_reused;
      stats_.mesh_named_values_read += mesh_metrics.named_values_read;
      stats_.mesh_named_values_reused += mesh_metrics.named_values_reused;
      if (velocity) {
        if (window.nominal != window.centre)
          data->nominal_pose = MeshNominalPose{data->positions, data->corner_normals, data->corner_uv,
                                               data->corner_colors, data->attributes};
        if (!velocity_motion(data->positions, data->motion, velocities, window, false, &accelerations))
          record.motion_warning =
              request.sop_path + " (mesh): accel gives no representable shutter position; using v only.";
        // Velocity describes positions, not a time derivative for an
        // authored shading normal. Hold those requested-frame normals
        // in object space across the shutter. Cycles motion triangles
        // require a normal buffer for every position step, including
        // when all authored values are constant. Generated normals keep
        // the engine's ordinary geometry-derived motion behavior.
        if (!data->corner_normals.empty())
          data->motion_corner_normals.assign(data->motion.positions.size(), data->corner_normals);
      }
      update.meshes.push_back(data);
      record.identifiers = std::move(fingerprint);
      record.revision = data->revision;
      record.material_key = request.material_key;
      ++stats_.extractions;
      stats_.conversion_seconds += elapsed(started);
    } else
      ++stats_.cache_hits;
  };
  const auto volume = [&](const GU_Detail &source, const GeometryRequest &request, const std::string &key,
                          const NativeGeometrySelection &selection) {
    if (window.enabled && request.motion == NativeGeometryMotion::Deformation)
      throw std::invalid_argument(
          "Volume deformation sampling requires a field interpolation method; use velocity blur: " + request.sop_path);
    desired.geometry.insert(key);
    const auto &frozen = prepared->volumes.at(key);
    auto fingerprint = frozen.identifiers;
    auto &record = cache_[key];
    if (captured.insert(key).second &&
        (record.identifiers != fingerprint || record.material_key != request.material_key)) {
      const auto started = Clock::now();
      if (!record.volume_conversion)
        record.volume_conversion = make_volume_conversion_cache();
      std::uint64_t dense_conversions = 0;
      auto data = convert_volume(*frozen.data, ++next_revision_, &record.motion_warning, &record.source_diagnostics,
                                 record.volume_conversion.get(), &dense_conversions);
      // Keep the read handles after conversion too. Otherwise a later
      // host edit could mutate the same dense buffer in place and leave
      // its pointer-based cache identity unchanged (or reuse its address).
      record.volume_capture = frozen.data;
      record.volume_source_allocations = volume_source_allocations(*frozen.data);
      stats_.dense_field_conversions += dense_conversions;
      record.volume_available = bool(data);
      if (!record.motion_warning.empty())
        record.motion_warning = request.sop_path + ": " + record.motion_warning;
      if (data)
        update.volumes.push_back(data);
      record.identifiers = std::move(fingerprint);
      record.revision = data ? data->revision : next_revision_;
      record.material_key = request.material_key;
      ++stats_.extractions;
      stats_.conversion_seconds += elapsed(started);
    } else
      ++stats_.cache_hits;
    if (!record.volume_available) {
      desired.unavailable_geometry.insert(key);
      desired.geometry.erase(key);
      std::erase_if(desired.objects.edit(), [&](const auto &item) { return item.second.geometry_key == key; });
    }
  };
  const auto points_curves = [&](const GU_Detail &source, const GeometryRequest &request, const std::string &key,
                                 bool curves, const NativeGeometrySelection &selection) {
    if (window.enabled && request.motion == NativeGeometryMotion::Deformation) {
      deformation(source, request, key, !curves, curves);
      return;
    }
    desired.geometry.insert(key);
    auto fingerprint = point_curve_identifiers(source, selection.rational_curves);
    if (!curves) {
      gaussian_splat_identifiers(fingerprint, source, request);
      fingerprint.push_back(request.point_splat_default_material);
    }
    named_attribute_identifiers(fingerprint, source, request);
    fingerprint.push_back(static_cast<std::int64_t>(request.material_mode));
    if (request.material_mode == NativeMaterialMode::Native)
      for (const auto owner : {GA_ATTRIB_POINT, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL, GA_ATTRIB_VERTEX})
        for (const auto *name : {"shop_materialpath", "material_override", "material_stylesheet"})
          fingerprint.push_back(id(source.findAttribute(owner, name)));
    const bool fallback = size_cache_.uses_fallback(source, selection);
    fingerprint.push_back(fallback ? std::bit_cast<std::int32_t>(curves ? request.curve_width : request.point_radius)
                                   : 0);
    if (selection.parametric_curves)
      fingerprint.push_back(request.curve_segments);
    velocity_fingerprint(fingerprint, source, request, window);
    auto &record = cache_[key];
    const bool valid = fingerprint[0] != GA_INVALID_DATAID && fingerprint[1] != GA_INVALID_DATAID &&
                       fingerprint[2] != GA_INVALID_DATAID;
    if (captured.insert(key).second &&
        (!valid || record.identifiers != fingerprint || record.material_key != request.material_key)) {
      if (unchanged_component(record, source, request, selection, key)) {
        record.identifiers = std::move(fingerprint);
        ++stats_.cache_hits;
      } else {
        const auto started = Clock::now();
        std::uint64_t fallback = 0;
        record.source_diagnostics.clear();
        std::vector<Float3> velocities, accelerations;
        auto *velocity_output =
            usable_velocity(source, request, selection, window, record.motion_warning) ? &velocities : nullptr;
        auto *acceleration_output =
            velocity_output && acceleration_attribute(source, request, window, &record.motion_warning) ? &accelerations
                                                                                                       : nullptr;
        if (curves) {
          auto data = std::make_shared<CurveData>(
              capture_curves(source, request, ++next_revision_, fallback, velocity_output, nullptr, true,
                             &record.material_references, &selection, &record.source_diagnostics, acceleration_output));
          data->key = key;
          data->instance_attributes = request.instance_attributes;
          if (velocity_output) {
            bool usable = velocities.size() == data->positions.size();
            if (!usable && (!velocities.empty() ||
                            std::none_of(record.source_diagnostics.begin(), record.source_diagnostics.end(),
                                         [](const auto &d) { return d.code == "geometry.invalid_velocity"; })))
              throw std::logic_error("Incomplete owned curve velocity correspondence");
            if (usable)
              for (std::size_t n = 0; n < velocities.size(); ++n)
                for (double delta : {window.deltas.front(), window.deltas.back()}) {
                  const auto &p = data->positions[n];
                  const auto &v = velocities[n];
                  usable &= std::isfinite(float(p.x + v.x * delta)) && std::isfinite(float(p.y + v.y * delta)) &&
                            std::isfinite(float(p.z + v.z * delta));
                }
            if (usable) {
              if (!velocity_motion(data->positions, data->motion, velocities, window, true, acceleration_output))
                record.motion_warning =
                    request.sop_path + " (curves): accel gives no representable shutter position; using v only.";
            } else
              record.motion_warning = request.sop_path +
                                      " (curves): velocity blur disabled; interpolated velocity has no representable "
                                      "shutter position. Rendering current geometry.";
          }
          record.revision = data->revision;
          record.primitive_count = data->first_key.size();
          update.curves.push_back(data);
        } else {
          auto data = std::make_shared<PointData>(
              capture_points(source, request, ++next_revision_, fallback, velocity_output, nullptr, true,
                             &record.material_references, &selection, &record.source_diagnostics, acceleration_output));
          data->key = key;
          data->instance_attributes = request.instance_attributes;
          if (velocity_output &&
              !velocity_motion(data->positions, data->motion, velocities, window, true, acceleration_output))
            record.motion_warning =
                request.sop_path + " (points): accel gives no representable shutter position; using v only.";
          record.revision = data->revision;
          record.primitive_count = data->positions.size();
          update.points.push_back(data);
        }
        record.index_ids = fallback;
        record.identifiers = std::move(fingerprint);
        record.material_key = request.material_key;
        ++stats_.extractions;
        stats_.conversion_seconds += elapsed(started);
      }
    } else
      ++stats_.cache_hits;
    if (curves)
      curve_count += record.primitive_count;
    else
      point_count += record.primitive_count;
    primitive_index_ids += record.index_ids;
  };
  const auto set_references = [&](const std::string &key, const std::vector<NativeMaterialReference> &references,
                                  bool sampled = false) {
    auto &stored = cache_[key].material_references;
    if (!sampled)
      stored = references;
    else
      for (auto &reference : stored)
        if (const auto found = std::find_if(references.begin(), references.end(),
                                            [&](const auto &current) { return current.key == reference.key; });
            found != references.end())
          reference = *found;
    // Sampled geometry keeps its centre bindings, with current paths for
    // unchanged material identities. Renames do not force a motion recook.
    for (const auto &reference : stored) {
      const auto paths = native_material_paths(reference);
      material_paths_.insert(paths.begin(), paths.end());
    }
  };
  // A packed prototype may be referenced by thousands of components. Its
  // source issue belongs to the resource/source pair, not each instance.
  std::set<std::pair<std::string, std::string>> diagnosed_components;
  const auto component_diagnostics = [&](const auto &record, const std::string &source, const std::string &key) {
    if (record.source_diagnostics.empty() && record.motion_warning.empty())
      return;
    if (!diagnosed_components.emplace(key, source).second)
      return;
    for (auto issue : record.source_diagnostics) {
      issue.source = source;
      issue.resource = key;
      update.source_diagnostics->push_back(std::move(issue));
    }
    if (!record.motion_warning.empty())
      update.source_diagnostics->push_back(
          {"motion.unavailable", source, key, record.motion_warning,
           "Rendering this component without the unsupported deformation or velocity effect"});
  };
  for (const auto &component : prepared->components) {
    if (!component.multiplicity)
      continue;
    const auto &source = *component.source;
    const auto &request = component.request;
    const auto &key = component.key;
    const auto &selection = *component.selection;
    window = component.window;
    cache_[key].instance_attributes = request.instance_attributes;
    if (selection.kind == NativeGeometryKind::Volume)
      volume(source, request, key, selection);
    else if (selection.kind == NativeGeometryKind::Mesh)
      mesh(source, request, key, selection);
    else
      points_curves(source, request, key, selection.kind == NativeGeometryKind::Curves, selection);
    if (component.multiplicity > 1) {
      if (selection.kind == NativeGeometryKind::Points)
        point_count += (component.multiplicity - 1) * cache_.at(key).primitive_count;
      if (selection.kind == NativeGeometryKind::Curves)
        curve_count += (component.multiplicity - 1) * cache_.at(key).primitive_count;
      if (selection.kind == NativeGeometryKind::Points || selection.kind == NativeGeometryKind::Curves)
        primitive_index_ids += (component.multiplicity - 1) * cache_.at(key).index_ids;
    }
    set_references(key, component.references,
                   window.enabled && (request.motion == NativeGeometryMotion::Deformation ||
                                      (request.native_materials && request.native_materials->elements)));
    component_diagnostics(cache_.at(key), request.sop_path, key);
  }
  for (const auto &root : prepared->retained_packed_roots)
    for (const auto &component : active_.packed_deformations.at(root).components) {
      const auto &record = cache_.at(component.key);
      ++stats_.cache_hits;
      if (component.kind == NativeGeometryKind::Points)
        point_count += component.multiplicity * record.primitive_count;
      if (component.kind == NativeGeometryKind::Curves)
        curve_count += component.multiplicity * record.primitive_count;
      if (component.kind == NativeGeometryKind::Points || component.kind == NativeGeometryKind::Curves)
        primitive_index_ids += component.multiplicity * record.index_ids;
      component_diagnostics(record, component.source, component.key);
    }
  for (auto &[path, failure] : prepared->invalid_packed_velocity) {
    failure.second.message += " (" + std::to_string(failure.first) + " affected packed instances in this source)";
    update.source_diagnostics->push_back(std::move(failure.second));
  }
  // Motion transforms must participate in the same transaction comparison as
  // the centre pose. Comparing before sampling would miss subframe edits.
  // Both tables are ordered. Match the snapshot layer's exact binding/pose
  // classification here, before copying unchanged attribute/property strings
  // into a second full ObjectData table merely to discard them for transport.
  convert_phase("convert_components");
  update.objects.clear();
  const auto comparison_started = Clock::now();
  // The ordered walk is split into key ranges compared in parallel. Each
  // range owns the accepted keys below the next range's first desired key, so
  // concatenating the ranges' records gives exactly the serial walk's lists.
  using ObjectEntry = std::pair<const std::string, ObjectData>;
  std::vector<const ObjectEntry *> current_objects, accepted_objects;
  // A recorded layout lists its table's nodes in key order; it is valid while
  // nothing was inserted or erased since (identity and revision unchanged).
  if (recycled && recycled_order.size() == desired.objects.size())
    current_objects = std::move(recycled_order);
  else {
    current_objects.reserve(desired.objects.size());
    for (const auto &entry : desired.objects)
      current_objects.push_back(&entry);
  }
  if (const auto &accepted = active_.packed_layout; accepted && accepted->table == active_.objects.identity() &&
                                                    accepted->revision == active_.objects.revision() &&
                                                    accepted->ordered.size() == active_.objects.size())
    accepted_objects = accepted->ordered;
  else {
    accepted_objects.reserve(active_.objects.size());
    for (const auto &entry : active_.objects)
      accepted_objects.push_back(&entry);
  }
  struct ComparisonPart {
    std::vector<ObjectData> objects;
    std::vector<ObjectPoseData> poses;
    std::vector<ObjectAttributeData> attributes;
    std::vector<std::string> deleted;
  };
  constexpr std::size_t comparison_chunk = 2048;
  const std::size_t comparison_parts =
      std::max<std::size_t>(1, (current_objects.size() + comparison_chunk - 1) / comparison_chunk);
  std::vector<ComparisonPart> parts(comparison_parts);
  const auto accepted_bound = [&](const std::string &key) {
    return std::size_t(
        std::lower_bound(accepted_objects.begin(), accepted_objects.end(), key,
                         [](const ObjectEntry *entry, const std::string &value) { return entry->first < value; }) -
        accepted_objects.begin());
  };
  const auto compare_part = [&](std::size_t part_index) {
    auto &part = parts[part_index];
    const auto first = part_index * comparison_chunk;
    const auto last = std::min(current_objects.size(), first + comparison_chunk);
    auto old = part_index == 0 ? std::size_t{0} : accepted_bound(current_objects[first]->first);
    const auto old_end =
        part_index + 1 == comparison_parts ? accepted_objects.size() : accepted_bound(current_objects[last]->first);
    for (auto position = first; position < last; ++position) {
      const auto &[key, data] = *current_objects[position];
      while (old != old_end && accepted_objects[old]->first < key) {
        part.deleted.push_back(accepted_objects[old]->first);
        ++old;
      }
      if (old == old_end || accepted_objects[old]->first != key) {
        part.objects.push_back(data);
        continue;
      }
      const auto &before = accepted_objects[old]->second;
      auto kind = classify_object_update(before, data);
      if (kind == ObjectUpdateKind::Attributes || kind == ObjectUpdateKind::PoseAttributes) {
        // Keep native generation conservative: schema or shutter-policy changes
        // take the existing complete object path. The public attribute DTO still
        // owns a complete replacement and performs full validation downstream.
        bool same_layout = before.attributes.size() == data.attributes.size();
        for (std::size_t i = 0; same_layout && i < data.attributes.size(); ++i) {
          const auto &previous = before.attributes[i], &current = data.attributes[i];
          same_layout = previous.name == current.name && previous.components == current.components &&
                        previous.motion_complete == current.motion_complete &&
                        previous.motion_values.size() == current.motion_values.size();
        }
        if (!same_layout)
          kind = ObjectUpdateKind::Full;
      }
      switch (kind) {
      case ObjectUpdateKind::Pose:
        part.poses.push_back({key, data.transform, data.motion, data.nominal_transform});
        break;
      case ObjectUpdateKind::Attributes:
        part.attributes.push_back({key, data.attributes});
        break;
      case ObjectUpdateKind::PoseAttributes:
        part.attributes.push_back(
            {key, data.attributes, ObjectPoseData{key, data.transform, data.motion, data.nominal_transform}});
        break;
      case ObjectUpdateKind::Full:
        part.objects.push_back(data);
        break;
      case ObjectUpdateKind::Unchanged:
        break;
      }
      ++old;
    }
    for (; old != old_end; ++old)
      part.deleted.push_back(accepted_objects[old]->first);
  };
  parallel_slots(comparison_parts, 1, [&](std::size_t begin, std::size_t end) {
    for (auto part = begin; part < end; ++part)
      compare_part(part);
  });
  const auto concatenate = [&](auto &target, auto member) {
    std::size_t size = target.size();
    for (const auto &part : parts)
      size += (part.*member).size();
    target.reserve(size);
    for (auto &part : parts)
      std::move((part.*member).begin(), (part.*member).end(), std::back_inserter(target));
  };
  concatenate(update.objects, &ComparisonPart::objects);
  concatenate(update.object_poses, &ComparisonPart::poses);
  concatenate(update.object_attributes, &ComparisonPart::attributes);
  concatenate(update.deleted_objects, &ComparisonPart::deleted);
  parts.clear();
  stats_.adapter_full_object_updates = update.objects.size();
  stats_.adapter_pose_updates = update.object_poses.size();
  stats_.adapter_attribute_updates = update.object_attributes.size();
  stats_.adapter_pose_attribute_updates =
      std::count_if(update.object_attributes.begin(), update.object_attributes.end(),
                    [](const auto &value) { return bool(value.pose); });
  if (const auto *value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY"); value && std::strcmp(value, "1") == 0)
    stats_.packed_assembly_seconds["conversion_object_comparison"] += elapsed(comparison_started);
  convert_phase("convert_comparison");
  for (const auto &key : active_.geometry)
    if (!desired.geometry.contains(key))
      update.deleted_geometry.push_back(key);
  for (const auto &key : desired.geometry)
    if (const auto record = cache_.find(key); record != cache_.end())
      desired.native_materials.emplace(key, record->second.material_references);
  stats_.motion_warnings.clear();
  for (const auto &key : desired.geometry)
    if (const auto record = cache_.find(key); record != cache_.end() && !record->second.motion_warning.empty())
      stats_.motion_warnings.push_back(record->second.motion_warning);
  if (layout) {
    layout->ordered = std::move(current_objects);
    layout->table = desired.objects.identity();
    layout->revision = desired.objects.revision();
    layout->origins = prepared->origins.get();
    desired.packed_layout = std::move(layout);
  }
  pending_ = std::move(desired);
  pending_ready_ = false;
  stats_.packed_instances = prepared->packed_instances;
  stats_.index_instance_ids = prepared->index_ids;
  stats_.points = point_count;
  stats_.curves = curve_count;
  stats_.index_primitive_ids = primitive_index_ids;
  convert_phase("convert_tail");
  return update;
}
SceneUpdate NativeSceneAdapter::capture(const std::vector<GeometryRequest> &requests, double time, bool authoritative,
                                        const NativeMotionSettings &motion,
                                        const NativeMaterialPreparation &prepare_materials) {
  auto update = convert(prepare(requests, time, authoritative, motion, prepare_materials));
  prepare_commit(update);
  return update;
}
ExtractionStats NativeSceneAdapter::stats() const {
  auto result = stats_;
  result.material_binding_scans = binding_cache_.scans();
  result.material_binding_cache_hits = binding_cache_.hits();
  result.lifecycle.retired_scenes = retired_ ? 1 : 0;
  result.lifecycle.retired_objects = retired_ ? retired_->objects.size() : 0;
  result.lifecycle.retired_geometry = retired_ ? retired_->geometry.size() : 0;
  result.partition_scans = partition_cache_.scans();
  result.partition_cache_hits = partition_cache_.hits();
  result.partition_cache_bytes = partition_cache_.bytes();
  std::set<const NativeMeshTopology *> topologies;
  for (const auto &[key, record] : cache_)
    if (record.mesh_capture)
      result.mesh_capture_cache_bytes +=
          mesh_capture_cache_bytes(*record.mesh_capture, topologies.insert(record.mesh_capture->topology.get()).second);
  std::set<std::uintptr_t> dense_allocations;
  for (const auto &[key, record] : cache_)
    for (const auto &[pointer, bytes] : record.volume_source_allocations)
      if (dense_allocations.insert(pointer).second)
        result.retained_dense_voxel_bytes += bytes;
  return result;
}
void NativeSceneAdapter::release_retired() {
  if (!retired_)
    return;
  const auto started = Clock::now();
  stats_.lifecycle.last_retired_objects = retired_->objects.size();
  // Keep a recyclable generation's object table, origins and leaf alias sets
  // for the next conversion (convert, PackedLayout); free everything else.
  spare_.reset();
  if (auto &layout = retired_->packed_layout) {
    auto spare = std::make_shared<PackedSpare>();
    spare->objects = std::move(retired_->objects);
    spare->navigation = std::move(retired_->navigation);
    for (const auto &alias : layout->aliases)
      if (auto found = retired_->object_aliases.find(alias); found != retired_->object_aliases.end())
        spare->aliases.emplace(alias, std::move(found->second));
    spare->layout = std::move(layout);
    spare_ = std::move(spare);
  }
  retired_.reset();
  ++stats_.lifecycle.retirements;
  stats_.lifecycle.retirement_seconds = elapsed(started);
  stats_.lifecycle.retirement_on_host = UT_Thread::isMainThread();
}
void NativeSceneAdapter::prepare_commit(const SceneUpdate &update) {
  // Camera/material/light-only commits cannot change aliases or retention.
  // Avoid walking every instance on Houdini's thread for a camera edit.
  if (!pending_ && update.deleted_objects.empty() && update.deleted_geometry.empty())
    return;
  if (pending_ready_)
    throw std::logic_error("Native scene publication was already prepared");
  if (retired_)
    throw std::logic_error("Native scene retirement must drain before preparing publication");
  const auto started = Clock::now();
  const auto retire_bindings = [&](Resources &desired) {
    for (auto it = cache_.begin(); it != cache_.end();)
      if (!desired.geometry.contains(it->first) && !desired.unavailable_geometry.contains(it->first))
        it = cache_.erase(it);
      else
        ++it;
    std::set<std::int64_t> retained_details;
    for (const auto &[key, details] : desired.geometry_details)
      retained_details.insert(details.begin(), details.end());
    std::erase_if(desired.binding_details, [&](const auto id) { return !retained_details.contains(id); });
    std::erase_if(desired.partition_details, [&](const auto id) { return !retained_details.contains(id); });
    style_cache_.retire(desired.binding_details);
    binding_cache_.retire(desired.binding_details);
    partition_cache_.retire(desired.partition_details);
    size_cache_.retire(desired.partition_details);
  };
  if (pending_population_reuse_) {
    if (update.deleted_objects.empty() && update.deleted_geometry.empty()) {
      // Even a geometry no-op can refresh material provenance or small source
      // retention tables. Object/alias maps and their immutable origins stay put.
      retire_bindings(*pending_);
      pending_ready_ = true;
      stats_.lifecycle.prepare_seconds = elapsed(started);
      return;
    }
    // Explicit partial deletions have independent semantics, including unknown
    // keys. Reconcile them normally and disable reuse of this accepted stamp.
    auto metadata = std::move(*pending_);
    pending_ = active_;
    pending_->packed_layout.reset();
    pending_->navigation = std::move(metadata.navigation);
    pending_->native_materials = std::move(metadata.native_materials);
    pending_population_reuse_ = false;
  }
  const bool captured = bool(pending_);
  // Explicit deletion-only updates have no acquired scene. Copy accepted
  // metadata under converter ownership; never mutate it before acceptance.
  if (!pending_) {
    pending_ = active_;
    pending_->packed_layout.reset();
  }
  auto &desired = *pending_;
  if (!update.deleted_objects.empty() || !update.deleted_geometry.empty()) {
    desired.direct_population.reset();
    desired.packed_population.reset();
  }
  bool removed = false;
  for (const auto &key : update.deleted_objects)
    removed = (desired.objects.erase(key) != 0) || removed;
  for (const auto &key : update.deleted_geometry) {
    removed = (desired.geometry.erase(key) != 0) || removed;
    desired.native_materials.erase(key);
    // A complete capture can retire an engine volume while retaining its
    // requested, currently unusable source. Direct source deletions have
    // no pending capture and must release that recovery state as well.
    if (!captured || !desired.unavailable_geometry.contains(key)) {
      removed = (desired.unavailable_geometry.erase(key) != 0) || removed;
      desired.geometry_details.erase(key);
    }
  }
  const auto prune = [](auto &aliases, const auto &resources) {
    for (auto it = aliases.begin(); it != aliases.end();) {
      std::erase_if(it->second, [&](const auto &key) { return !resources.contains(key); });
      if (it->second.empty())
        it = aliases.erase(it);
      else
        ++it;
    }
  };
  // Fresh authoritative aliases already contain only desired resources.
  // A separate explicit deletion can require pruning; computed old-resource
  // deletions are absent from the fresh state and need no full instance scan.
  if (removed) {
    desired.packed_layout.reset();
    auto sources = desired.geometry;
    sources.insert(desired.unavailable_geometry.begin(), desired.unavailable_geometry.end());
    prune(desired.geometry_aliases, sources);
    prune(desired.object_aliases, desired.objects);
    std::erase_if(desired.instance_fields,
                  [&](const auto &value) { return !desired.object_aliases.contains(value.first); });
  }
  retire_bindings(desired);
  pending_ready_ = true;
  stats_.lifecycle.prepare_seconds = elapsed(started);
}
void NativeSceneAdapter::commit() {
  if (!pending_)
    return;
  if (!pending_ready_ || retired_)
    throw std::logic_error("Native scene publication is not ready or its retirement slot is occupied");
  const auto started = Clock::now();
  if (pending_population_reuse_) {
    auto &next = *pending_;
    active_.navigation = std::move(next.navigation);
    active_.native_materials = std::move(next.native_materials);
    active_.geometry_details = std::move(next.geometry_details);
    active_.geometry_aliases = std::move(next.geometry_aliases);
    active_.binding_details = std::move(next.binding_details);
    active_.partition_details = std::move(next.partition_details);
    active_.direct_population = std::move(next.direct_population);
    active_.packed_population = std::move(next.packed_population);
    pending_.reset();
    pending_population_reuse_ = pending_ready_ = false;
    ++stats_.lifecycle.publications;
    stats_.lifecycle.publication_seconds = elapsed(started);
    return;
  }
  retired_.emplace(std::move(active_));
  active_ = std::move(*pending_);
  pending_.reset();
  pending_ready_ = false;
  ++stats_.lifecycle.publications;
  stats_.lifecycle.publication_seconds = elapsed(started);
}
std::shared_ptr<const NativeObjectTable::Table>
NativeSceneAdapter::snapshot_objects(const std::shared_ptr<const NativeObjectTable::Table> &previous,
                                     const SceneUpdate &update) const {
  // snapshot_state(previous, update) applies this update's object records to
  // the previous table. The update is the difference from the accepted table
  // to the prepared one, plus the explicit deletions that prepare_commit has
  // already applied to the prepared table. So when the previous table is the
  // accepted one, the result is the prepared table, unless a deleted key is
  // also written by the update (snapshot_state keeps an upsert, prepare_commit
  // drops it, and a deleted pose/attribute record is rejected there).
  if (pending_ && !pending_ready_)
    return {};
  if (!(previous && active_.objects.owns(previous)) && !((!previous || previous->empty()) && active_.objects.empty()))
    return {};
  const bool records = !update.objects.empty() || !update.object_poses.empty() || !update.object_attributes.empty();
  if (!pending_ && (records || !update.deleted_objects.empty()))
    return {};
  if (!update.deleted_objects.empty()) {
    std::unordered_set<std::string_view> deleted(update.deleted_objects.begin(), update.deleted_objects.end());
    const auto written = [&](const auto &values) {
      return std::any_of(values.begin(), values.end(), [&](const auto &value) { return deleted.contains(value.key); });
    };
    if (written(update.objects) || written(update.object_poses) || written(update.object_attributes))
      return {};
  }
  // A certified population keeps the accepted table: its update has no object records.
  if (!pending_ || pending_population_reuse_)
    return records ? nullptr : active_.objects.share();
  return pending_->objects.share();
}
void NativeSceneAdapter::expand_deletions(SceneUpdate &update) const {
  const auto expand = [&](auto &deleted, const auto &active_aliases, const auto *pending_aliases) {
    std::set<std::string> physical;
    for (const auto &key : deleted) {
      bool found = false;
      for (const auto *aliases : {&active_aliases, pending_aliases})
        if (aliases) {
          if (const auto it = aliases->find(key); it != aliases->end()) {
            physical.insert(it->second.begin(), it->second.end());
            found = true;
          }
        }
      if (!found)
        physical.insert(key);
    }
    deleted.assign(physical.begin(), physical.end());
  };
  expand(update.deleted_geometry, active_.geometry_aliases, pending_ ? &pending_->geometry_aliases : nullptr);
  expand(update.deleted_objects, active_.object_aliases, pending_ ? &pending_->object_aliases : nullptr);
}
std::vector<NativeMaterialReference> NativeSceneAdapter::material_references() const {
  const auto &resources = pending_ ? *pending_ : active_;
  std::map<std::string, NativeMaterialReference> references;
  for (const auto &[key, entries] : resources.native_materials)
    if (resources.geometry.contains(key))
      for (const auto &reference : entries)
        references[reference.key] = reference;
  std::vector<NativeMaterialReference> result;
  for (const auto &[key, reference] : references)
    result.push_back(reference);
  return result;
}
} // namespace cycles_houdini
