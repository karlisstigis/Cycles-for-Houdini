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
#include <GU/GU_PrimPacked.h>
#include <GA/GA_Handle.h>
#include <OP/OP_Operator.h>
#include <OP/OP_OperatorTable.h>
#include <PRM/PRM_Include.h>
#include <PRM/PRM_Conditional.h>
#include <PY/PY_Python.h>
#include <SOP/SOP_Node.h>
#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <vector>

namespace cycles_houdini {
namespace {
// The original single-reference tokens are retained for old HIP files.
PRM_Name file_name("file", "Blender File"), kind_name("target_kind", "Object / Collection"),
    target_name("target", "Target Name"), frame_name("frame", "Source Frame"),
    refresh_name("refresh_bounds", "Refresh"), catalogue_refresh_name("refresh_targets", "Refresh Target Menu"),
    multiple_name("multiple_references", "Multiple References"), rows_name("references", "References"),
    enabled_name("ref_enabled#", "Enabled"), instance_name("ref_name#", "Name"),
    override_name("ref_file#", "Blender File Override"), row_kind_name("ref_target_kind#", "Object / Collection"),
    row_target_name("ref_target#", "Name"), row_frame_name("ref_frame#", "Source Frame"),
    initialized_name("references_initialized", "References Initialized"),
    catalogue_name("saved_catalogue", "Saved Catalogue"),
    bindings_name("saved_target_bindings", "Saved Target Bindings"),
    migration_name("saved_reference_migration", "Saved Reference Migration"),
    viewport_name("viewport_shape", "Viewport Shape"), member_hull_name("ref_hull_per_member#", "Hull per member"),
    shape_frame_name("shape_frame", "Shape Frame"), hull_vertices_name("max_hull_vertices", "Maximum Hull Vertices"),
    wireframe_name("viewport_wireframe", "Wireframe"), preview_name("saved_viewport_hulls", "Saved Viewport Previews"),
    frame_initialized_name("ref_frame_initialized#", "Source Frame Initialized"),
    catalogue_status_name("catalogue_status", "Target Menu Status"), bounds_name("saved_bounds", "Saved Bounds"),
    bounds_status_name("saved_bounds_status", "Bounds Status"),
    include_disabled_name("ref_include_disabled_collections#", "Include Disabled Collections");
std::string hexString(const std::string &value) {
  constexpr char digits[] = "0123456789abcdef";
  std::string hex;
  for (const unsigned char c : value) {
    hex += digits[c >> 4];
    hex += digits[c & 15];
  }
  return hex;
}
std::string pythonNode(const OP_Node *node) {
  return "__import__('hou').node(bytes.fromhex('" + hexString(node->getFullPath().toStdString()) +
         "').decode('utf-8'))";
}
int callPython(void *data, const char *module, const char *function) {
  const auto *node = static_cast<OP_Node *>(data);
  const auto code = std::string("import hou\nfrom cycles_houdini.blender.") + module + " import " + function + "\n" +
                    function + "(" + pythonNode(node) + ")\n";
  PYrunPythonStatementsAndExpectNoErrors(code.c_str(), "Cycles Blender Reference");
  return 1;
}
int refreshBounds(void *data, int, fpreal64, const PRM_Template *) {
  return callPython(data, "blend_catalogue", "refresh_node");
}
int refreshTargets(void *data, int, fpreal64, const PRM_Template *) {
  const auto code = "import hou\nfrom cycles_houdini.blender.blend_catalogue import request_catalogues\n"
                    "request_catalogues(" +
                    pythonNode(static_cast<OP_Node *>(data)) + ", force=True)\n";
  PYrunPythonStatementsAndExpectNoErrors(code.c_str(), "Cycles Blender target catalogue");
  return 1;
}
int parametersChanged(void *data, int, fpreal64, const PRM_Template *) {
  return callPython(data, "blend_catalogue", "parameters_changed");
}
int selectionChanged(void *data, int, fpreal64, const PRM_Template *) {
  return callPython(data, "blend_catalogue", "selection_changed");
}
int viewportChanged(void *data, int, fpreal64, const PRM_Template *) {
  return callPython(data, "blend_catalogue", "viewport_changed");
}
int memberHullChanged(void *data, int, fpreal64, const PRM_Template *) {
  const auto code = pythonNode(static_cast<OP_Node *>(data)) + ".cook(force=True)";
  PYrunPythonStatementsAndExpectNoErrors(code.c_str(), "Cycles Blender viewport shape");
  return 1;
}
void targetMenu(void *data, PRM_Name *names, int size, const PRM_SpareData *, const PRM_Parm *parm) {
  if (size < 1)
    return;
  const std::string token = parm ? parm->getToken() : "target";
  const auto expression =
      "__import__('cycles_houdini.blender.blend_catalogue', fromlist=['cached_menu_entries']).cached_menu_entries(" +
      pythonNode(static_cast<OP_Node *>(data)) + ", bytes.fromhex('" + hexString(token) + "').decode('utf-8'))";
  const auto result = PYrunPythonExpression(expression.c_str(), PY_Result::STRING_ARRAY);
  int index = 0;
  if (result.myResultType == PY_Result::STRING_ARRAY) {
    const auto &entries = result.myStringArray;
    for (exint i = 0; i + 1 < entries.size() && index < size - 1; i += 2)
      names[index++] = PRM_Name(PRM_Name::COPY, entries[i], entries[i + 1]);
  }
  names[index].setAsSentinel();
}
PRM_Name kinds[]{PRM_Name("object", "Object"), PRM_Name("collection", "Collection"), PRM_Name()};
PRM_ChoiceList kind_menu(PRM_CHOICELIST_SINGLE, kinds), target_menu(PRM_CHOICELIST_APPEND, targetMenu);
PRM_Name viewport_shapes[]{PRM_Name("bounds", "Bounding Box"), PRM_Name("hull", "Convex Hull"), PRM_Name()};
PRM_ChoiceList viewport_menu(PRM_CHOICELIST_SINGLE, viewport_shapes);
PRM_Default viewport_default(0, "bounds");
PRM_Default kind_default(0, "object"), frame_default(1);
PRM_Default hull_vertices_default(64);
// A convenience slider must never clamp explicitly entered source samples.
PRM_Range frame_range(PRM_RANGE_UI, 1, PRM_RANGE_UI, 100);
PRM_Range hull_vertices_range(PRM_RANGE_UI, 8, PRM_RANGE_UI, 256);
PRM_Conditional collection_hull_only("{ ref_target_kind# != collection }", PRM_CONDTYPE_HIDE);
PRM_Conditional hull_detail_only("{ viewport_shape != hull }", PRM_CONDTYPE_DISABLE);
PRM_Template row_templates[]{
    PRM_Template(PRM_TOGGLE, 1, &enabled_name, PRMoneDefaults, nullptr, nullptr, parametersChanged),
    PRM_Template(PRM_STRING, 1, &row_kind_name, &kind_default, &kind_menu, nullptr, selectionChanged),
    PRM_Template(PRM_STRING, 1, &row_target_name, nullptr, &target_menu, nullptr, selectionChanged, nullptr, 1,
                 "Blender names or underscore aliases; use spaces between targets, quotes for exact names, and "
                 "wildcards for patterns."),
    PRM_Template(PRM_FLT, 1, &row_frame_name, &frame_default, nullptr, &frame_range, selectionChanged, nullptr, 1,
                 "Blender timeline frame for rendered geometry and conservative render bounds. Shape Frame "
                 "independently selects the frozen viewport preview."),
    PRM_Template(PRM_TOGGLE, 1, &member_hull_name, PRMzeroDefaults, nullptr, nullptr, memberHullChanged),
    // Legacy row channels remain serializable after the visible controls.
    PRM_Template(PRM_STRING, 1, &instance_name),
    PRM_Template(PRM_FILE, 1, &override_name, nullptr, nullptr, nullptr, parametersChanged),
    PRM_Template(PRM_TOGGLE, 1, &frame_initialized_name, PRMzeroDefaults),
    // Appended so existing row indices stay stable; shown after Hull per member.
    PRM_Template(PRM_TOGGLE, 1, &include_disabled_name, PRMzeroDefaults, nullptr, nullptr, selectionChanged, nullptr, 1,
                 "Also include child collections that are disabled (excluded) in the source file's view layer."),
    PRM_Template()};
PRM_Template templates[]{
    PRM_Template(PRM_FILE, 1, &file_name, nullptr, nullptr, nullptr, parametersChanged),
    PRM_Template(PRM_CALLBACK, 1, &refresh_name, nullptr, nullptr, nullptr, refreshBounds, nullptr, 1,
                 "Refresh Blender targets and evaluate enabled selections asynchronously for their viewport boxes."),
    PRM_Template(PRM_STRING, 1, &viewport_name, &viewport_default, &viewport_menu, nullptr, viewportChanged),
    PRM_Template(PRM_TOGGLE, 1, &wireframe_name, PRMoneDefaults, nullptr, nullptr, memberHullChanged, nullptr, 1,
                 "Draw viewport hull edges without shaded faces. Does not change rendering."),
    PRM_Template(
        PRM_FLT, 1, &shape_frame_name, &frame_default, nullptr, &frame_range, viewportChanged, nullptr, 1,
        "Blender timeline frame used only for the viewport box or hull. Keep this fixed to freeze the preview while "
        "Source Frame animates the render geometry. Finite fractional and out-of-slider frames are supported."),
    PRM_Template(PRM_INT, 1, &hull_vertices_name, &hull_vertices_default, nullptr, &hull_vertices_range,
                 viewportChanged, nullptr, 1,
                 "Maximum vertices for each object hull, each member hull, or the whole-collection hull. Default 64; "
                 "slider 8 to 256; explicit values 8 to 4096. Oversized selections report the aggregate preview guard "
                 "instead of silently reducing member detail. Does not affect rendering."),
    PRM_Template(PRM_MULTITYPE_LIST, row_templates, 1, &rows_name, PRMzeroDefaults, nullptr, nullptr,
                 "Each enabled row selects Blender objects or collections. Multiple names and wildcard patterns "
                 "produce separate packed primitives.",
                 nullptr, parametersChanged),
    // Preserve old parameter tokens and expressions without duplicate visible controls.
    PRM_Template(PRM_STRING, 1, &kind_name, &kind_default, &kind_menu, nullptr, selectionChanged),
    PRM_Template(PRM_STRING, 1, &target_name, nullptr, &target_menu, nullptr, selectionChanged),
    PRM_Template(PRM_FLT, 1, &frame_name, &frame_default, nullptr, &frame_range, selectionChanged),
    PRM_Template(PRM_CALLBACK, 1, &catalogue_refresh_name, nullptr, nullptr, nullptr, refreshTargets),
    PRM_Template(PRM_STRING, 1, &catalogue_status_name),
    PRM_Template(PRM_TOGGLE, 1, &multiple_name, PRMzeroDefaults, nullptr, nullptr, parametersChanged),
    PRM_Template(PRM_STRING, 1, &bounds_status_name), PRM_Template(PRM_STRING, 1, &bounds_name),
    PRM_Template(PRM_STRING, 1, &catalogue_name), PRM_Template(PRM_TOGGLE, 1, &initialized_name, PRMzeroDefaults),
    PRM_Template(PRM_STRING, 1, &bindings_name), PRM_Template(PRM_STRING, 1, &migration_name),
    PRM_Template(PRM_STRING, 1, &preview_name), PRM_Template()};
struct ConfigureTemplates {
  ConfigureTemplates() {
    templates[0].setJoinNext(true);
    templates[2].setJoinNext(true);
    templates[5].setConditionalBasePtr(&hull_detail_only);
    for (int index = 7; index < 20; ++index)
      templates[index].setInvisible(true);
    row_templates[0].setJoinNext(true);
    row_templates[0].setLabelNone(true);
    row_templates[1].setLabelNone(true);
    row_templates[4].setConditionalBasePtr(&collection_hull_only);
    row_templates[5].setInvisible(true);
    row_templates[6].setInvisible(true);
    row_templates[7].setInvisible(true);
    row_templates[8].setConditionalBasePtr(&collection_hull_only);
  }
} configure_templates;

class BlenderReferenceSop final : public SOP_Node {
public:
  BlenderReferenceSop(OP_Network *net, const char *name, OP_Operator *op) : SOP_Node(net, name, op) {}
  static OP_Node *construct(OP_Network *net, const char *name, OP_Operator *op) {
    return new BlenderReferenceSop(net, name, op);
  }

  void loadFinished() override {
    SOP_Node::loadFinished();
    callPython(this, "blend_catalogue", "migrate_node");
  }

  bool runCreateScript() override {
    if (!SOP_Node::runCreateScript())
      return false;
    setInt("references", 0, 0.0, 1);
    setInt("multiple_references", 0, 0.0, 1);
    setInt("references_initialized", 0, 0.0, 1);
    // Native defaults also cover nodes created without their creation script.
    callPython(this, "blend_catalogue", "initialize_rows");
    return true;
  }

protected:
  OP_ERROR cookMySop(OP_Context &context) override {
    const auto time = context.getTime();
    gdp->clearAndDestroy();
    UT_String saved_bounds;
    evalStringRaw(saved_bounds, "saved_bounds", 0, time);
    // Resolve through the same cache-only path as bounds refresh. The result
    // carries exact Blender IDs separately from Houdini's normalized piece names.
    std::ostringstream evaluation_time;
    evaluation_time << std::setprecision(17) << time;
    const auto expression = "__import__('cycles_houdini.blender.blend_catalogue', fromlist=['resolve_node_references'])"
                            ".resolve_node_references(" +
                            pythonNode(this) + ", time=" + evaluation_time.str() + ")";
    const auto result = PYrunPythonExpression(expression.c_str(), PY_Result::STRING_ARRAY);
    if (result.myResultType != PY_Result::STRING_ARRAY || result.myStringArray.size() < 2 ||
        (result.myStringArray.size() - 2) % 10 != 0) {
      addError(SOP_MESSAGE, "Cannot resolve Blender references.");
      return error();
    }
    const auto &values = result.myStringArray;
    if (values[0].length() > 0) {
      addError(SOP_MESSAGE, values[0].c_str());
      return error();
    }
    if (values[1].length() > 0)
      addWarning(SOP_MESSAGE, values[1].c_str());
    struct Row {
      UT_String file{UT_String::ALWAYS_DEEP}, kind{UT_String::ALWAYS_DEEP}, target{UT_String::ALWAYS_DEEP},
          name{UT_String::ALWAYS_DEEP}, hull{UT_String::ALWAYS_DEEP}, shape_bounds{UT_String::ALWAYS_DEEP};
      double frame, shape_frame;
      bool per_member = false, wireframe = true;
    };
    std::vector<Row> rows;
    for (exint i = 2; i < values.size(); i += 10) {
      Row row;
      row.file = values[i];
      row.kind = values[i + 1];
      row.target = values[i + 2];
      char *end = nullptr;
      row.frame = std::strtod(values[i + 3].c_str(), &end);
      row.name = values[i + 4];
      row.per_member = values[i + 5] == "1";
      row.hull = values[i + 6];
      char *shape_end = nullptr;
      row.shape_frame = std::strtod(values[i + 7].c_str(), &shape_end);
      row.shape_bounds = values[i + 8];
      row.wireframe = values[i + 9] == "1";
      if (!end || end == values[i + 3].c_str() || *end || !std::isfinite(row.frame) || !shape_end ||
          shape_end == values[i + 7].c_str() || *shape_end || !std::isfinite(row.shape_frame) ||
          (row.kind != "object" && row.kind != "collection" && row.kind != "collection+disabled")) {
        addError(SOP_MESSAGE, "Invalid resolved Blender reference.");
        return error();
      }
      rows.push_back(std::move(row));
    }
    GA_RWHandleS name_attribute(gdp->addStringTuple(GA_ATTRIB_PRIMITIVE, "name", 1));
    UT_String viewport_shape;
    evalString(viewport_shape, "viewport_shape", 0, time);
    for (const auto &row : rows) {
      auto *prim = GU_PrimPacked::build(*gdp, PackedBlenderReference::typeName);
      if (!prim) {
        addError(SOP_MESSAGE, "Cannot create Cycles Blender Reference primitive.");
        return error();
      }
      UT_Options options;
      options.setOptionS("cycles_blend_file", row.file);
      options.setOptionS("cycles_blend_target", row.target);
      options.setOptionS("cycles_blend_target_kind", row.kind);
      options.setOptionF("cycles_blender_frame", row.frame);
      options.setOptionS("cycles_blender_viewport_shape", viewport_shape);
      options.setOptionS("cycles_blender_viewport_hull", row.hull);
      options.setOptionF("cycles_blender_shape_frame", row.shape_frame);
      options.setOptionS("cycles_blender_shape_bounds", row.shape_bounds);
      options.setOptionF("cycles_blender_viewport_wireframe", row.wireframe ? 1.0 : 0.0);
      options.setOptionF("cycles_blender_hull_per_member", row.per_member ? 1.0 : 0.0);
      BlenderReferenceBounds bounds;
      const std::string reference =
          row.file.toStdString() + "#" + row.kind.toStdString() + "=" + row.target.toStdString();
      if (lookup_blender_reference_bounds(reference, row.frame, bounds))
        options.setOptionS("cycles_blender_bounds", bounds.serialized);
      else if (select_saved_blender_reference_bounds(saved_bounds.toStdString(), reference, row.frame, bounds))
        options.setOptionS("cycles_blender_bounds_preview", bounds.serialized);
      prim->hardenImplementation()->update(prim, options);
      const auto *impl = packed_blender_reference(*prim);
      const bool has_hull = impl && impl->getPackedDetail().isValid();
      prim->setViewportLOD(viewport_shape == "hull" && has_hull ? GEO_VIEWPORT_FULL : GEO_VIEWPORT_BOX);
      if (viewport_shape == "hull" && !has_hull)
        addWarning(SOP_MESSAGE, "Viewport hull is unavailable; showing the bounding box. Refresh to evaluate it.");
      gdp->setPos3(prim->getPointOffset(0), UT_Vector3(0.0f));
      if (name_attribute.isValid())
        name_attribute.set(prim->getMapOffset(), row.name);
      if (!row.file.isstring() || !row.target.isstring())
        addWarning(SOP_MESSAGE, "Choose a Blender file and target name. Bounds remain a unit box until evaluated.");
    }
    callPython(this, "node_bounds", "cook_node");
    return error();
  }
};
} // namespace
} // namespace cycles_houdini

void newSopOperator(OP_OperatorTable *table) {
  table->addOperator(new OP_Operator("cycles::blender_reference", "Cycles Blender Reference",
                                     cycles_houdini::BlenderReferenceSop::construct, cycles_houdini::templates, 0, 0,
                                     nullptr, OP_FLAG_GENERATOR));
}
