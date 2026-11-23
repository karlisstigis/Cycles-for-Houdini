// SPDX-License-Identifier: Apache-2.0
// Lowering of the native Switch, Index Switch, Enable Output and Warning nodes
// into plain Cycles shader nodes. Constant selectors pick their branch here
// (constant values come from shader_constant_value, shader_primitives.cpp).
// The conversion of selectors to Booleans and the graph built for
// non-constant selectors (boolean_constant, boolean_endpoint, lower_varying)
// were rewritten on 21 November 2026 from the behaviour description in
// docs/dev/provenance/shader-switches.md, after an earlier version that
// followed Blender's GPL source closely was removed; see that document's
// Provenance section for how the rewrite was made and reviewed.
#include "shader_switches.h"
#include "owned_ramp.h"
#include <bit>
#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <set>
#include <stdexcept>

namespace cycles_houdini {
namespace {
// Houdini OP_MULTI_INPUT_MAX is 9999, including the selector socket.
constexpr int maximum_items = 9998;
const std::map<std::string, std::string> types = {{"float", "float"},   {"integer", "int"}, {"boolean", "boolean"},
                                                  {"vector", "vector"}, {"color", "color"}, {"shader", "closure"}};
ShaderValue zero(const std::string &type) {
  if (type == "int") {
    return 0;
  }
  if (type == "boolean") {
    return false;
  }
  if (type == "vector" || type == "color") {
    return std::array<float, 3>{0, 0, 0};
  }
  return 0.f;
}
ShaderSocketDefinition socket(std::string id, std::string label, std::string type, bool linkable = true) {
  ShaderSocketDefinition s;
  s.id = std::move(id);
  s.label = std::move(label);
  s.type = std::move(type);
  s.linkable = linkable;
  if (s.type != "closure") {
    s.authoring_default = zero(s.type);
  }
  s.default_json = s.type == "boolean"                       ? "false"
                   : s.type == "vector" || s.type == "color" ? "[0,0,0]"
                   : s.type == "closure"                     ? "null"
                                                             : "0";
  return s;
}
struct Kind {
  bool index{};
  bool enable{};
  std::string type;
};
std::optional<Kind> kind(const std::string &definition) {
  for (const auto &[suffix, type] : types) {
    for (bool index : {false, true}) {
      if (definition == std::string("cycles/") + (index ? "index_switch_" : "switch_") + suffix + "/1") {
        return Kind{index, false, type};
      }
    }
    if (definition == "cycles/enable_output_" + suffix + "/1") {
      return Kind{false, true, type};
    }
  }
  return {};
}
using Value = std::variant<ShaderValue, ShaderEndpoint>;
bool is_vector_type(const std::string &type) { return type == "vector" || type == "point" || type == "normal"; }
// A constant selector value as a Boolean, or nullopt when it must go through
// the graph (source_type: the output type of the node it comes from).
// Numbers are true when greater than zero (NaN compares false, so it is
// false); vectors are true when their length is above zero. A colour (or
// an untyped triple) is left to the graph, where Cycles' own colour-to-float
// luminance decides it.
std::optional<ShaderValue> boolean_constant(const ShaderValue &value, const std::string &source_type) {
  if (const auto *b = std::get_if<bool>(&value)) {
    return ShaderValue{*b};
  }
  if (const auto *i = std::get_if<int>(&value)) {
    return ShaderValue{*i > 0};
  }
  if (const auto *f = std::get_if<float>(&value)) {
    return ShaderValue{*f > 0.f};
  }
  if (const auto *v = std::get_if<std::array<float, 3>>(&value)) {
    if (!is_vector_type(source_type)) {
      return std::nullopt;
    }
    // Same test as the graph path (vector length, then "> 0"), so a constant
    // and a varying selector agree also for NaN or underflowing components.
    const float length = std::sqrt((*v)[0] * (*v)[0] + (*v)[1] * (*v)[1] + (*v)[2] * (*v)[2]);
    return ShaderValue{length > 0.f};
  }
  throw std::invalid_argument("Boolean selector requires numeric input");
}
// The Cycles node that blends two values of a switch type with a 0/1 factor,
// and its socket names. Integer and Boolean values travel as floats: Cycles
// converts at the sockets, exactly for magnitudes up to 2^24.
struct Blend {
  const char *definition;
  const char *first;
  const char *second;
  const char *output;
  const char *socket_type;
};
Blend blend_for(const std::string &type) {
  if (type == "closure") {
    return {"cycles/mix_closure/1", "closure1", "closure2", "closure", "closure"};
  }
  if (type == "vector") {
    return {"cycles/mix_vector/1", "a", "b", "result", "vector"};
  }
  if (type == "color") {
    return {"cycles/mix_color/1", "a", "b", "result", "color"};
  }
  return {"cycles/mix_float/1", "a", "b", "result", "float"};
}
} // namespace
bool shader_switch_definition(const std::string &id) { return bool(kind(id)) || id == "cycles/warning/1"; }
std::vector<ShaderDefinition> shader_switch_definitions() {
  std::vector<ShaderDefinition> result;
  for (const auto &[suffix, type] : types) {
    for (bool index : {false, true}) {
      ShaderDefinition d;
      d.engine_type = std::string(index ? "index_switch_" : "switch_") + suffix;
      d.id = "cycles/" + d.engine_type + "/1";
      d.inputs.push_back(socket(index ? "index" : "switch", index ? "Index" : "Switch", index ? "int" : "boolean"));
      if (index) {
        auto count = socket("item_count", "Items", "int", false);
        count.authoring_default = 2;
        count.default_json = "2";
        d.inputs.push_back(count);
        // Prototype item: native authoring instantiates a zero-based
        // multiparm and matching dynamic sockets, without reindexing wires.
        d.inputs.push_back(socket("item0", "0", type));
      } else {
        d.inputs.push_back(socket("false", "False", type));
        d.inputs.push_back(socket("true", "True", type));
      }
      auto out = socket("output", "Output", type);
      out.authoring_default.reset();
      out.default_json = "null";
      d.outputs.push_back(out);
      result.push_back(std::move(d));
    }
    ShaderDefinition enable;
    enable.engine_type = "enable_output_" + suffix;
    enable.id = "cycles/" + enable.engine_type + "/1";
    auto enabled = socket("enable", "Enable", "boolean");
    enabled.authoring_default = true;
    enabled.default_json = "true";
    enable.inputs = {enabled, socket("value", "Value", type)};
    auto output = socket("output", "Output", type);
    output.authoring_default.reset();
    output.default_json = "null";
    enable.outputs = {output};
    result.push_back(std::move(enable));
  }
  ShaderDefinition warning;
  warning.engine_type = "warning";
  warning.id = "cycles/warning/1";
  auto show = socket("show", "Show", "boolean");
  show.authoring_default = true;
  show.default_json = "true";
  auto message = socket("message", "Message", "string", false);
  message.authoring_default = std::string("Warning");
  message.default_json = "\"Warning\"";
  auto severity = socket("severity", "Severity", "enum", false);
  severity.authoring_default = std::string("warning");
  severity.default_json = "\"warning\"";
  severity.enum_values = {{"info", 0}, {"warning", 1}, {"error", 2}};
  warning.inputs = {show, message, severity};
  auto shown = socket("show", "Show", "boolean");
  shown.authoring_default.reset();
  shown.default_json = "null";
  warning.outputs = {shown};
  result.push_back(std::move(warning));
  return result;
}
ShaderGraphData lower_shader_switches(const ShaderGraphData &source, std::vector<SourceDiagnostic> *diagnostics) {
  if (std::none_of(source.nodes.begin(), source.nodes.end(),
                   [](const auto &n) { return shader_switch_definition(n.definition); })) {
    return source;
  }
  if (source.nodes.size() > 256 || source.links.size() > 2048) {
    throw std::invalid_argument("Shader switch source exceeds graph limits");
  }
  ShaderGraphData result = source;
  result.nodes.clear();
  result.links.clear();
  const auto add_node = [&](ShaderNodeData node) {
    if (result.nodes.size() >= 256) {
      throw std::invalid_argument("Inlined shader switches exceed the 256-node engine budget");
    }
    result.nodes.push_back(std::move(node));
  };
  const auto connect = [&](ShaderEndpoint from, ShaderEndpoint to) {
    if (result.links.size() >= 2048) {
      throw std::invalid_argument("Inlined shader switches exceed the 2048-link engine budget");
    }
    result.links.push_back({std::move(from), std::move(to)});
  };
  std::map<std::string, const ShaderNodeData *> nodes;
  std::map<std::pair<std::string, std::string>, ShaderEndpoint> inputs;
  std::set<std::string> names, executing, retained;
  unsigned serial = 0;
  for (const auto &n : source.nodes) {
    if (!nodes.emplace(n.id, &n).second) {
      throw std::invalid_argument("Duplicate shader switch source node");
    }
    names.insert(n.id);
  }
  for (const auto &link : source.links) {
    if (!inputs.emplace(std::pair(link.to.node, link.to.socket), link.from).second) {
      throw std::invalid_argument("Duplicate shader switch input connection");
    }
  }
  const auto unique = [&]() {
    std::string id;
    do {
      id = "_switch_inline" + std::to_string(serial++);
    } while (!names.insert(id).second);
    return id;
  };
  const auto append = [&](std::string definition, std::map<std::string, ShaderValue> parameters) {
    auto id = unique();
    add_node({id, std::move(definition), std::move(parameters)});
    return id;
  };
  const auto constant = [&](const ShaderValue &value, const std::string &type) -> ShaderEndpoint {
    if (type == "closure") {
      const auto id = append("cycles/emission/1", {{"color", std::array<float, 3>{0, 0, 0}}, {"strength", 0.f}});
      return {id, "emission"};
    }
    if (const auto *v = std::get_if<int>(&value)) {
      const auto id =
          append("cycles/integer_math/1", {{"math_type", std::string("add")}, {"value1", *v}, {"value2", 0}});
      return {id, "value"};
    }
    if (const auto *v = std::get_if<bool>(&value)) {
      const auto id =
          append("cycles/boolean_math/1", {{"math_type", std::string("or")}, {"boolean1", int(*v)}, {"boolean2", 0}});
      return {id, "boolean"};
    }
    if (const auto *v = std::get_if<std::array<float, 3>>(&value)) {
      const auto id = append("cycles/color/1", {{"value", *v}});
      return {id, "color"};
    }
    const auto id = append("cycles/value/1", {{"value", value}});
    return {id, "value"};
  };
  const auto bind = [&](const Value &value, const std::string &id, const std::string &input, const std::string &type) {
    if (const auto *link = std::get_if<ShaderEndpoint>(&value)) {
      connect(*link, {id, input});
    } else if (type == "closure") {
      connect(constant(std::get<ShaderValue>(value), type), {id, input});
    } else {
      auto n = std::find_if(result.nodes.begin(), result.nodes.end(), [&](const auto &n) { return n.id == id; });
      auto parameter = std::get<ShaderValue>(value);
      if (type == "float") {
        if (const auto *boolean = std::get_if<bool>(&parameter)) {
          parameter = float(*boolean);
        }
      }
      n->parameters[input] = std::move(parameter);
    }
  };
  std::map<std::pair<std::string, std::string>, ShaderEndpoint> memo;
  std::function<ShaderEndpoint(const ShaderEndpoint &)> resolve;
  static const auto output_types = [] {
    std::map<std::pair<std::string, std::string>, std::string> result;
    for (const auto &definition : shader_definitions()) {
      for (const auto &output : definition.outputs) {
        result.emplace(std::pair(definition.id, output.id), output.type);
      }
    }
    return result;
  }();
  // A non-constant selector endpoint of another type as a 0/1 endpoint.
  // Vectors: length > 0. Everything else, colours and unknown types included,
  // goes straight into a float comparison "> 0"; Cycles converts a colour
  // linked to a float input with its colour-management luminance.
  const auto boolean_endpoint = [&](const ShaderEndpoint &endpoint, const std::string &source_type) -> ShaderEndpoint {
    ShaderEndpoint number = endpoint;
    if (is_vector_type(source_type)) {
      const auto length = append("cycles/vector_math/1", {{"math_type", std::string("length")}});
      connect(endpoint, {length, "vector1"});
      number = {length, "value"};
    }
    const auto positive = append("cycles/math/1", {{"math_type", std::string("greater_than")}, {"value2", 0.f}});
    connect(number, {positive, "value1"});
    return {positive, "value"};
  };
  const auto input = [&](const ShaderNodeData &node, const std::string &name, ShaderValue fallback,
                         bool boolean = false) -> Value {
    const auto found = inputs.find({node.id, name});
    if (found != inputs.end()) {
      const auto source_node = nodes.find(found->second.node);
      std::string source_type;
      if (source_node != nodes.end()) {
        const auto type = output_types.find({source_node->second->definition, found->second.socket});
        if (type != output_types.end()) {
          source_type = type->second;
        }
      }
      if (auto primitive = shader_constant_value(source, found->second)) {
        if (!boolean) {
          return *primitive;
        }
        if (auto selected = boolean_constant(*primitive, source_type)) {
          return *selected;
        }
      }
      ShaderEndpoint endpoint = resolve(found->second);
      if (boolean && source_type != "boolean") {
        endpoint = boolean_endpoint(endpoint, source_type);
      }
      return endpoint;
    }
    const auto p = node.parameters.find(name);
    return p == node.parameters.end() ? std::move(fallback) : p->second;
  };
  const auto number = [](const ShaderValue &v) {
    if (const auto *n = std::get_if<int>(&v)) {
      return double(*n);
    }
    if (const auto *n = std::get_if<float>(&v)) {
      return double(*n);
    }
    if (const auto *n = std::get_if<bool>(&v)) {
      return double(*n);
    }
    throw std::invalid_argument("Switch selector requires a numeric value");
  };
  // Varying selector: Switch and Enable Output (count 2, branches select(0) and
  // select(1), selector already 0/1) or Index Switch (count items, raw index).
  //
  // Switch and Enable Output are one blend with the selector as factor.
  //
  // Index Switch is a binary search (a balanced decision tree, as in binary
  // search over a sorted table: Knuth, TAOCP vol. 3, section 6.2.1) over the
  // integer n = trunc(index). n itself is never computed: each split tests
  // "n >= k" for a whole number k directly on the index x (see at_least).
  // The leaves, in order, are
  //   zero, item 0, item 1, ..., item count-1, zero
  // and leaf j (j = 0 .. count+1) owns the integers n with n >= j-1 that are
  // below the next leaf's start, i.e. leaf 0 owns n < 0, leaf k+1 owns n == k,
  // leaf count+1 owns n >= count. A subtree over the leaves [lo, hi) splits at
  // mid = (lo + hi) / 2: the right half starts where n >= mid-1. Every n
  // therefore reaches exactly one leaf: item[n] when 0 <= n < count, zero
  // below and above. NaN fails every comparison and lands on the leftmost
  // (zero) leaf; +-infinity land on the zero leaves too.
  // Nodes: count+1 comparisons and count+1 blends, depth about
  // log2(count + 2).
  const auto lower_varying = [&](const Kind &kind, const Value &selector, int count,
                                 const std::function<Value(int)> &select) -> ShaderEndpoint {
    const Blend b = blend_for(kind.type);
    const auto mix = [&](const Value &factor, const Value &low, const Value &high) -> ShaderEndpoint {
      const auto id = append(b.definition, {});
      bind(factor, id, "fac", "float");
      bind(low, id, b.first, b.socket_type);
      bind(high, id, b.second, b.socket_type);
      return {id, b.output};
    };
    if (!kind.index) {
      return mix(selector, select(0), select(1));
    }
    // Colour and vector Index Switch whose items are all finite constants:
    // a table lookup instead of the decision tree below (3 nodes for any
    // count instead of 2 * count + 2). The 257-entry table (the owned Color
    // Ramp table, constant interpolation) holds zero at entry 0, item i at
    // entry i + 1 and zero above. The factor (trunc(x) + 1.5) / 256 makes the
    // engine's lookup, entry = int(clamp(factor, 0, 1) * 256), pick entry
    // trunc(x) + 1 when 0 <= trunc(x) < count; negative indices clamp to entry
    // 0 and large ones to entry 256, both zero; NaN also gives entry 0. Both
    // steps are exact in 32-bit floats (multiples of 1/512).
    if ((kind.type == "color" || kind.type == "vector") && count <= 255) {
      std::vector<std::array<float, 3>> items;
      for (int i = 0; i < count; ++i) {
        const auto value = select(i);
        const auto *constant = std::get_if<ShaderValue>(&value);
        const auto *triple = constant ? std::get_if<std::array<float, 3>>(constant) : nullptr;
        if (!triple || !std::all_of(triple->begin(), triple->end(), [](float v) { return std::isfinite(v); })) {
          items.clear();
          break;
        }
        items.push_back(*triple);
      }
      if (int(items.size()) == count) {
        std::vector<int> table = {ramp_source_magic, 8, 257, 0, 1, 0, 0, 0};
        table.resize(8 + 257 * 3, 0); // +0.0f is all zero bits
        for (int i = 0; i < count; ++i) {
          for (int c = 0; c < 3; ++c) {
            table[8 + 3 * (i + 1) + c] = std::bit_cast<int>(items[i][c]);
          }
        }
        const auto whole = append("cycles/math/1", {{"math_type", std::string("trunc")}});
        bind(selector, whole, "value1", "float");
        const auto factor =
            append("cycles/math/1",
                   {{"math_type", std::string("multiply_add")}, {"value2", 1.f / 256.f}, {"value3", 1.5f / 256.f}});
        connect({whole, "value"}, {factor, "value1"});
        const auto lookup = append("cycles/owned_ramp/1", {{"source", std::move(table)}});
        connect({factor, "value"}, {lookup, "factor"});
        return {lookup, "color"};
      }
    }
    // 0/1 factor for trunc(x) >= k, k whole, as one "x > t" comparison on the
    // index itself (saves the trunc node). For k >= 1 truncation only moves
    // positive x down to a whole number, so the test is x >= k, written as
    // x > (largest float below k): the index and the comparison are 32-bit
    // floats in the engine, so this is the same test. For k <= 0 truncation
    // moves x in (k-1, k) up to k, so the test is x > k - 1, exact.
    const auto at_least = [&](int k) {
      const float threshold = k >= 1 ? std::nextafter(float(k), -std::numeric_limits<float>::infinity()) : float(k - 1);
      const auto id = append("cycles/math/1", {{"math_type", std::string("greater_than")}, {"value2", threshold}});
      bind(selector, id, "value1", "float");
      return ShaderEndpoint{id, "value"};
    };
    // One shared zero for both outer leaves (for closures this is a node).
    const Value nothing = kind.type == "closure" ? Value{constant(zero(kind.type), kind.type)} : Value{zero(kind.type)};
    const auto leaf = [&](int j) { return j == 0 || j == count + 1 ? nothing : select(j - 1); };
    std::function<Value(int, int)> subtree = [&](int lo, int hi) -> Value {
      if (hi - lo == 1) {
        return leaf(lo);
      }
      const int mid = lo + (hi - lo) / 2;
      const Value low = subtree(lo, mid);
      const Value high = subtree(mid, hi);
      return mix(at_least(mid - 1), low, high);
    };
    return std::get<ShaderEndpoint>(subtree(0, count + 2));
  };
  resolve = [&](const ShaderEndpoint &endpoint) -> ShaderEndpoint {
    const auto key = std::pair(endpoint.node, endpoint.socket);
    if (const auto found = memo.find(key); found != memo.end()) {
      return found->second;
    }
    const auto found = nodes.find(endpoint.node);
    if (found == nodes.end()) {
      throw std::invalid_argument("Missing shader switch source node: " + endpoint.node);
    }
    if (!executing.insert(endpoint.node).second) {
      throw std::invalid_argument("Cyclic shader switch source");
    }
    const auto &node = *found->second;
    ShaderEndpoint out = endpoint;
    if (node.definition == "cycles/warning/1") {
      for (const auto &[name, value] : node.parameters) {
        if (name != "show" && name != "message" && name != "severity") {
          throw std::invalid_argument("Unknown Warning parameter: " + name);
        }
      }
      if (endpoint.socket != "show") {
        throw std::invalid_argument("Unknown Warning output");
      }
      const auto shown = input(node, "show", true, true);
      bool show = false;
      if (const auto *primitive = std::get_if<ShaderValue>(&shown)) {
        show = number(*primitive) > 0;
        if (show) {
          const auto message = node.parameters.find("message");
          const auto severity = node.parameters.find("severity");
          const auto *text = message == node.parameters.end() ? nullptr : std::get_if<std::string>(&message->second);
          const auto *level = severity == node.parameters.end() ? nullptr : std::get_if<std::string>(&severity->second);
          if ((message != node.parameters.end() && !text) || (severity != node.parameters.end() && !level)) {
            throw std::invalid_argument("Warning requires owned message/severity strings");
          }
          if (level && *level != "info" && *level != "warning" && *level != "error") {
            throw std::invalid_argument("Unknown Warning severity");
          }
          if (diagnostics) {
            diagnostics->push_back({"material.warning", node.id, "", text ? *text : "Warning", "Authored Warning node",
                                    level ? *level : "warning", "message"});
          }
        }
      } else if (diagnostics) {
        diagnostics->push_back({"material.warning_requires_constant", node.id, "",
                                "Warnings only work when inputs are constant values",
                                "Using false output without evaluating the message", "info", "show"});
      }
      out = constant(ShaderValue{show}, "boolean");
    } else if (const auto k = kind(node.definition)) {
      if (endpoint.socket != "output") {
        throw std::invalid_argument("Unknown shader switch output");
      }
      for (const auto &[name, value] : node.parameters) {
        bool branch = false;
        if (k->index && name.starts_with("item") && name != "item_count") {
          const auto token = name.substr(4);
          branch = !token.empty() && token.find_first_not_of("0123456789") == std::string::npos && token.size() <= 4 &&
                   std::stoi(token) < maximum_items;
        } else if (!k->index) {
          branch = k->enable ? name == "value" : name == "false" || name == "true";
        }
        if (branch) {
          const bool valid =
              k->type == "int"                            ? std::holds_alternative<int>(value)
              : k->type == "boolean"                      ? std::holds_alternative<bool>(value)
              : k->type == "vector" || k->type == "color" ? std::holds_alternative<std::array<float, 3>>(value)
              : k->type == "float" ? (std::holds_alternative<float>(value) || std::holds_alternative<int>(value))
                                   : false;
          if (!valid) {
            throw std::invalid_argument("Switch branch parameter type mismatch: " + name);
          }
        } else if (name != (k->index    ? "index"
                            : k->enable ? "enable"
                                        : "switch") &&
                   !(k->index && name == "item_count")) {
          throw std::invalid_argument("Unknown switch parameter: " + name);
        }
      }
      const auto selector = input(node,
                                  k->index    ? "index"
                                  : k->enable ? "enable"
                                              : "switch",
                                  k->index ? ShaderValue{0} : ShaderValue{k->enable}, !k->index);
      int count = 2;
      if (k->index) {
        const auto p = node.parameters.find("item_count");
        if (p != node.parameters.end()) {
          const auto *value = std::get_if<int>(&p->second);
          if (!value) {
            throw std::invalid_argument("Index Switch item_count requires integer");
          }
          count = *value;
        }
        if (count < 0 || count > maximum_items) {
          throw std::invalid_argument("Index Switch exceeds Houdini's 9999-input limit (selector included)");
        }
      }
      const auto select = [&](int i) {
        if (k->enable && i == 0) {
          return Value{zero(k->type)};
        }
        return input(node,
                     k->index    ? "item" + std::to_string(i)
                     : k->enable ? "value"
                     : i == 0    ? "false"
                                 : "true",
                     zero(k->type), k->type == "boolean");
      };
      if (count == 0) {
        out = constant(zero(k->type), k->type);
      } else if (const auto *value = std::get_if<ShaderValue>(&selector)) {
        const double n = number(*value);
        if (!std::isfinite(n)) {
          throw std::invalid_argument("Nonfinite switch selector");
        }
        const double index = k->index ? std::trunc(n) : (n > 0 ? 1 : 0);
        if (k->index && (index < 0 || index >= count) && diagnostics) {
          diagnostics->push_back({"material.index_switch_out_of_range", node.id, "",
                                  "Constant Index Switch index is out of range",
                                  "Using the output type's zero fallback", "warning", "index"});
        }
        Value selected = index < 0 || index >= count ? Value{zero(k->type)} : select(int(index));
        out = std::holds_alternative<ShaderEndpoint>(selected) ? std::get<ShaderEndpoint>(selected)
                                                               : constant(std::get<ShaderValue>(selected), k->type);
      } else {
        out = lower_varying(*k, selector, count, select);
      }
    } else {
      if (retained.insert(node.id).second) {
        add_node(node);
        for (const auto &link : source.links) {
          if (link.to.node == node.id) {
            const auto from = resolve(link.from);
            connect(from, link.to);
          }
        }
      }
    }
    executing.erase(endpoint.node);
    memo.emplace(key, out);
    return out;
  };
  if (!source.surface.node.empty()) {
    result.surface = resolve(source.surface);
  }
  if (source.volume) {
    result.volume = resolve(*source.volume);
  }
  if (source.displacement) {
    result.displacement = resolve(*source.displacement);
  }
  for (const auto &node : source.nodes) {
    if (node.definition == "cycles/warning/1") {
      resolve({node.id, "show"});
    }
    if (node.definition == "cycles/aov_output/1" && retained.insert(node.id).second) {
      add_node(node);
      for (const auto &link : source.links) {
        if (link.to.node == node.id) {
          connect(resolve(link.from), link.to);
        }
      }
    }
  }
  if (result.nodes.size() > 256 || result.links.size() > 2048) {
    throw std::invalid_argument("Inlined shader switches exceed graph limits");
  }
  return result;
}
} // namespace cycles_houdini
