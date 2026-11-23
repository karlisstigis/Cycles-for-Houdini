// SPDX-License-Identifier: Apache-2.0
// Read-only HDK probe for the viewport collector; never replaces a collector.
#include <UT/UT_DSOVersion.h>
#include <OP/OP_Director.h>
#include <SOP/SOP_Node.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimPacked.h>
#include <GA/GA_Handle.h>
#include <GT/GT_GEODetail.h>
#include <GT/GT_PrimCollect.h>
#include <GT/GT_PrimInstance.h>
#include <GT/GT_Refine.h>
#include <GT/GT_RefineParms.h>
#include <cmath>
#include <set>
#include <sstream>
#include <stdexcept>

namespace {
class Probe final : public GT_Refine {
public:
  const GU_Detail *outer = nullptr;
  int batches = 0, entries = 0, inner_sources = 0, inner_prototypes = 0;
  int names = 0, colors = 0, primitive_ids = 0, vertex_ids = 0, errors = 0;
  double matrix_error = 0;
  std::set<GA_Offset> offsets;
  bool allowThreading() const override { return false; }
  void addPrimitive(const GT_PrimitiveHandle &primitive) override {
    if (const auto *collection = dynamic_cast<const GT_PrimCollect *>(primitive.get())) {
      for (exint index = 0; index < collection->entries(); ++index)
        addPrimitive(collection->getPrim(index));
      return;
    }
    const auto *instance = dynamic_cast<const GT_PrimInstance *>(primitive.get());
    if (!instance)
      return;
    ++batches;
    entries += int(instance->entries());
    const auto &source = instance->sourceGeometry();
    const auto *prototype = dynamic_cast<const GT_GEODetail *>(instance->geometry().get());
    const auto attrs = instance->getUniformAttributes();
    GA_ROHandleS point_names(outer->findPointAttribute("name"));
    GA_ROHandleS primitive_names(outer->findPrimitiveAttribute("name"));
    GA_ROHandleV3 point_colors(outer->findPointAttribute("Cd"));
    bool all_sources_match = true, all_prototypes_match = true;
    if (instance->packedPrimOffsets().entries() != instance->entries()) {
      ++errors;
      return;
    }
    for (exint index = 0; index < instance->entries(); ++index) {
      const auto offset = instance->packedPrimOffsets()(index);
      offsets.insert(offset);
      const auto *packed = dynamic_cast<const GU_PrimPacked *>(outer->getPrimitive(offset));
      if (!packed) {
        ++errors;
        continue;
      }
      const auto hull = packed->getPackedDetail();
      // The viewport update path reads the source geometry of the prototype,
      // not the animated outer Copy to Points detail.
      all_sources_match &= source && source->entries() == 1 && source->getGeometry(0).gdp() == hull.gdp();
      all_prototypes_match &= prototype && prototype->getGeometry().gdp() == hull.gdp();
      UT_Matrix4D expected, actual;
      packed->getFullTransform4(expected);
      instance->transforms()->get(index)->getMatrix(actual);
      for (int row = 0; row < 4; ++row)
        for (int column = 0; column < 4; ++column)
          matrix_error = std::max(matrix_error, std::abs(expected(row, column) - actual(row, column)));
      const auto point = packed->getPointOffset(0);
      if (attrs && attrs->hasName("name")) {
        ++names;
        const auto expected_name = point_names.isValid() ? point_names.get(point) : primitive_names.get(offset);
        errors += attrs->get("name")->getS(index) != expected_name;
      }
      if (attrs && attrs->hasName("Cd") && point_colors.isValid()) {
        ++colors;
        const auto expected_color = point_colors.get(point);
        for (int component = 0; component < 3; ++component)
          errors += std::abs(attrs->get("Cd")->getF64(index, component) - expected_color(component)) > 1e-6;
      }
      if (attrs && attrs->hasName("__primitive_id")) {
        ++primitive_ids;
        errors += attrs->get("__primitive_id")->getI64(index) != offset;
      }
      if (attrs && attrs->hasName("__vertex_id")) {
        ++vertex_ids;
        errors += attrs->get("__vertex_id")->getI64(index) != packed->getVertexOffset(0);
      }
    }
    inner_sources += all_sources_match;
    inner_prototypes += all_prototypes_match;
  }
};
thread_local std::string result;
} // namespace

extern "C" __declspec(dllexport) const char *probe_blender_viewport_gt(const char *path, double time, int viewport,
                                                                       int override_lod) {
  try {
    auto *sop = OPgetDirector()->findSOPNode(path);
    if (!sop)
      throw std::runtime_error("Missing SOP");
    OP_Context context(time);
    const auto detail = sop->getCookedGeoHandle(context);
    if (!detail)
      throw std::runtime_error("Missing geometry");
    GT_RefineParms parms;
    parms.setPackedViewportLOD(viewport != 0);
    parms.setOverridePackedViewportLOD(override_lod);
    Probe probe;
    probe.outer = detail.gdp();
    GT_GEODetail::makeDetail(detail)->refine(probe, &parms);
    std::ostringstream output;
    output << "{\"batches\":" << probe.batches << ",\"entries\":" << probe.entries
           << ",\"inner_sources\":" << probe.inner_sources << ",\"inner_prototypes\":" << probe.inner_prototypes
           << ",\"unique_offsets\":" << probe.offsets.size() << ",\"names\":" << probe.names
           << ",\"colors\":" << probe.colors << ",\"primitive_ids\":" << probe.primitive_ids
           << ",\"vertex_ids\":" << probe.vertex_ids << ",\"errors\":" << probe.errors
           << ",\"matrix_error\":" << probe.matrix_error << "}";
    result = output.str();
  } catch (const std::exception &) {
    result = "{\"error\":\"Native GT probe failed\"}";
  }
  return result.c_str();
}
