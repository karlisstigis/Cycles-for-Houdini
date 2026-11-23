// SPDX-License-Identifier: Apache-2.0
#include "stylesheet_evaluation.h"
#include "stylesheet_sources.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <filesystem>
#include <fstream>
#include <deque>
#include <map>
#include <set>
#include <stdexcept>
#include <sstream>
#include <CVEX/CVEX_Context.h>
#include <OP/OP_Director.h>
#include <UT/UT_OpCaller.h>
#include <UT/UT_JSONWriter.h>
#include <VCC/VCC_Utils.h>
#include <VEX/VEX_VexResolver.h>
#include <STY/STY_Binding.h>
#include <STY/STY_BindingDataInfo.h>
#include <STY/STY_Override.h>
#include <STY/STY_OverrideBlock.h>
#include <STY/STY_OverrideCategory.h>
#include <STY/STY_OverrideScript.h>
#include <STY/STY_ResultsFilter.h>
#include <STY/STY_StylerGroup.h>
#include <STY/STY_SubjectGroup.h>
#include <STY/STY_Subject.h>
#include <STY/STY_TargetMatchStatus.h>
#include <UT/UT_IStream.h>
#include <UT/UT_JSONValue.h>
#include <UT/UT_WorkArgs.h>
#include <UT/UT_OptionEntry.h>
#include <UT/UT_ErrorManager.h>
#include <UT/UT_PathSearch.h>

namespace cycles_houdini {
namespace {
using Field = std::pair<std::string, std::string>;
using Rejections = std::map<const STY_Style *, std::map<Field, std::string>>;
bool supported(const UT_StringHolder &category) {
  return category == STY_OVERRIDE_MAT || category == STY_OVERRIDE_MAT_PARMS || category == STY_OVERRIDE_SURFACE_PARMS ||
         category == STY_OVERRIDE_DISPLACE_PARMS;
}
std::string marker(const Field &field) { return std::to_string(field.first.size()) + ":" + field.first + field.second; }
void remove(UT_JSONValue &value, const std::string &name) { value.erase(name.c_str()); }
UT_JSONValue &map(UT_JSONValue &parent, const std::string &name) {
  auto &child = parent[name.c_str()];
  if (!child.isMap())
    child.startMap();
  return child;
}
bool finite(const STY_BindingDataSourceHandle &source, STY_BindingDataInfo &info) {
  if (!source || !source->getOverrideBindingInfo(info) || info.isArray() || info.getTupleSize() < 1 ||
      info.getTupleSize() > 4)
    return false;
  if (info.getDataType() == STY_BindingDataInfo::TYPE_FLOAT) {
    UT_FprealArray data;
    if (!source->getOverrideBindingData(data) || data.size() != info.getTupleSize())
      return false;
    for (auto value : data)
      if (!std::isfinite(value) || std::abs(value) > 3.402823466e38)
        return false;
    return true;
  }
  if (info.getDataType() == STY_BindingDataInfo::TYPE_INT) {
    UT_IntArray data;
    return source->getOverrideBindingData(data) && data.size() == info.getTupleSize();
  }
  UT_StringArray data;
  if (!source->getOverrideBindingData(data) || data.size() != 1)
    return false;
  const auto value = data[0].toStdString();
  return value.size() <= 65536 && value.find('\0') == std::string::npos;
}
CVEX_Type type(const STY_BindingDataInfo &info) {
  if (info.getDataType() == STY_BindingDataInfo::TYPE_INT)
    return info.getTupleSize() == 1 ? CVEX_TYPE_INTEGER : CVEX_TYPE_INVALID;
  if (info.getDataType() == STY_BindingDataInfo::TYPE_STRING)
    return info.getTupleSize() == 1 ? CVEX_TYPE_STRING : CVEX_TYPE_INVALID;
  switch (info.getTupleSize()) {
  case 1:
    return CVEX_TYPE_FLOAT;
  case 2:
    return CVEX_TYPE_VECTOR2;
  case 3:
    return CVEX_TYPE_VECTOR3;
  case 4:
    return CVEX_TYPE_VECTOR4;
  default:
    return CVEX_TYPE_INVALID;
  }
}
bool usable(const STY_ConstantHandle &value) {
  if (!value || value->optionSize() < 1 || value->optionSize() > 4)
    return false;
  if (value->optionStorage() == UT_OptionEntry::UT_OPTION_STORE_REAL) {
    UT_Array<fpreal64> data;
    if (!value->importOption(data))
      return false;
    for (auto number : data)
      if (!std::isfinite(number) || std::abs(number) > 3.402823466e38)
        return false;
    return true;
  }
  if (value->optionStorage() == UT_OptionEntry::UT_OPTION_STORE_INT ||
      value->optionStorage() == UT_OptionEntry::UT_OPTION_STORE_BOOL)
    return true;
  if (value->optionStorage() == UT_OptionEntry::UT_OPTION_STORE_STRING) {
    UT_StringHolder text;
    if (value->optionSize() != 1 || !value->importOption(text))
      return false;
    const auto owned = text.toStdString();
    return owned.size() <= 65536 && owned.find('\0') == std::string::npos;
  }
  return false;
}
struct CollectStyles final : STY_ResultsFilter {
  mutable std::vector<const STY_Style *> styles;
  bool acceptsCategory(const UT_StringHolder &) const override { return false; }
  bool acceptsFurtherOverrides(const STY_Style &style) const override {
    styles.push_back(&style);
    return true;
  }
};
struct Program {
  std::string error;
  std::map<std::string, CVEX_Type> inputs;
  CVEX_Function function;
  UT_StringHolder output;
  CVEX_Type output_type{CVEX_TYPE_INVALID};
  std::vector<std::string> arguments{"cycles_stylesheet"};
  std::string cwd;
};
struct StyleValue {
  CVEX_Type type{CVEX_TYPE_INVALID};
  UT_FprealArray real;
  UT_IntArray integer;
  UT_StringArray text;
};
StyleValue read_value(const STY_BindingDataSourceHandle &source) {
  STY_BindingDataInfo info;
  StyleValue value;
  if (!finite(source, info))
    return value;
  value.type = type(info);
  if (info.getDataType() == STY_BindingDataInfo::TYPE_INT)
    source->getOverrideBindingData(value.integer);
  else if (info.getDataType() == STY_BindingDataInfo::TYPE_STRING)
    source->getOverrideBindingData(value.text);
  else
    source->getOverrideBindingData(value.real);
  return value;
}
struct ScriptValue final : STY_BindingDataSource {
  const Program *program{};
  std::map<std::string, StyleValue> inputs;
  StyleValue result;
  std::string error;
  int cwd{-1};
  mutable bool needed{};
  bool complete{};
  bool getOverrideBindingInfo(STY_BindingDataInfo &info) const override {
    const auto t = program->output_type;
    info.setIsArray(false);
    info.setTupleSize(CVEX_Value::getTupleSize(t));
    info.setDataType(t == CVEX_TYPE_INTEGER  ? STY_BindingDataInfo::TYPE_INT
                     : t == CVEX_TYPE_STRING ? STY_BindingDataInfo::TYPE_STRING
                                             : STY_BindingDataInfo::TYPE_FLOAT);
    return true;
  }
  bool getOverrideBindingData(UT_FprealArray &data) const override {
    needed = true;
    if (!error.empty())
      return false;
    data = result.real;
    return true;
  }
  bool getOverrideBindingData(UT_IntArray &data) const override {
    needed = true;
    if (!error.empty())
      return false;
    data = result.integer;
    return true;
  }
  bool getOverrideBindingData(UT_StringArray &data) const override {
    needed = true;
    if (!error.empty())
      return false;
    data = result.text;
    return true;
  }
};
struct Buffer {
  CVEX_Type type{CVEX_TYPE_INVALID};
  std::vector<int> integer;
  std::vector<float> real;
  std::vector<UT_Vector2F> v2;
  std::vector<UT_Vector3F> v3;
  std::vector<UT_Vector4F> v4;
  CVEX_StringArray text;
  Buffer(CVEX_Type type, int size) : type(type) {
    switch (type) {
    case CVEX_TYPE_INTEGER:
      integer.resize(size);
      break;
    case CVEX_TYPE_FLOAT:
      real.resize(size);
      break;
    case CVEX_TYPE_VECTOR2:
      v2.assign(size, UT_Vector2F(0.f, 0.f));
      break;
    case CVEX_TYPE_VECTOR3:
      v3.assign(size, UT_Vector3F(0.f, 0.f, 0.f));
      break;
    case CVEX_TYPE_VECTOR4:
      v4.assign(size, UT_Vector4F(0.f, 0.f, 0.f, 0.f));
      break;
    case CVEX_TYPE_STRING:
      text.setSize(size);
      break;
    default:
      throw std::logic_error("Unsupported owned CVEX buffer type");
    }
  }
  bool bind(CVEX_Value &value) {
    const int n =
        int(std::max({integer.size(), real.size(), v2.size(), v3.size(), v4.size(), std::size_t(text.size())}));
    switch (type) {
    case CVEX_TYPE_INTEGER:
      return value.setTypedData(integer.data(), n);
    case CVEX_TYPE_FLOAT:
      return value.setTypedData(real.data(), n);
    case CVEX_TYPE_VECTOR2:
      return value.setTypedData(v2.data(), n);
    case CVEX_TYPE_VECTOR3:
      return value.setTypedData(v3.data(), n);
    case CVEX_TYPE_VECTOR4:
      return value.setTypedData(v4.data(), n);
    case CVEX_TYPE_STRING:
      return value.setTypedData(text);
    default:
      return false;
    }
  }
  void set(int i, const StyleValue &value) {
    switch (type) {
    case CVEX_TYPE_INTEGER:
      integer[i] = value.integer[0];
      break;
    case CVEX_TYPE_FLOAT:
      real[i] = float(value.type == CVEX_TYPE_INTEGER ? value.integer[0] : value.real[0]);
      break;
    case CVEX_TYPE_VECTOR2:
      for (int c = 0; c < 2; ++c)
        v2[i][c] = float(value.real[c]);
      break;
    case CVEX_TYPE_VECTOR3:
      for (int c = 0; c < 3; ++c)
        v3[i][c] = float(value.real[c]);
      break;
    case CVEX_TYPE_VECTOR4:
      for (int c = 0; c < 4; ++c)
        v4[i][c] = float(value.real[c]);
      break;
    case CVEX_TYPE_STRING:
      text[i] = value.text[0];
      break;
    default:
      break;
    }
  }
  StyleValue get(int i) const {
    StyleValue value;
    value.type = type;
    switch (type) {
    case CVEX_TYPE_INTEGER:
      value.integer.append(integer[i]);
      break;
    case CVEX_TYPE_FLOAT:
      value.real.append(real[i]);
      break;
    case CVEX_TYPE_VECTOR2:
      for (int c = 0; c < 2; ++c)
        value.real.append(v2[i][c]);
      break;
    case CVEX_TYPE_VECTOR3:
      for (int c = 0; c < 3; ++c)
        value.real.append(v3[i][c]);
      break;
    case CVEX_TYPE_VECTOR4:
      for (int c = 0; c < 4; ++c)
        value.real.append(v4[i][c]);
      break;
    case CVEX_TYPE_STRING:
      value.text.append(text[i]);
      break;
    default:
      break;
    }
    return value;
  }
};
struct Definition {
  const STY_Style *origin{};
  STY_StyleHandle flat;
  std::map<Field, STY_OverrideScriptHandle> scripts;
};
void refresh_program(const std::string &command, const std::string &resource) {
  // Native command loading retains object code until its function is
  // explicitly invalidated. Do this only for a changed source, at the host
  // acquisition boundary; never flush unrelated Houdini CVEX functions.
  static std::map<std::string, std::vector<std::string>> observed;
  const auto stamp = stylesheet_source_signature({resource});
  const auto found = observed.find(resource);
  if (found != observed.end() && found->second == stamp)
    return;
  CVEX_Context::clearFunction(UT_StringHolder(command));
  if (resource.starts_with("file:") && command != resource.substr(5))
    CVEX_Context::clearFunction(UT_StringHolder(resource.substr(5)));
  if (observed.size() >= 256)
    observed.erase(observed.begin());
  observed.insert_or_assign(resource, stamp);
}
} // namespace
struct NativeStyleEvaluation::Impl {
  double time;
  std::string cwd;
  Impl(double time, const std::string &cwd) : time(time), cwd(cwd) {}
  bool scripted{};
  std::map<const STY_StyleSheet *, STY_StyleSheetHandle> sheets;
  std::vector<STY_StyleSheetHandle> retained;
  std::map<const STY_Style *, Definition> definitions;
  std::map<std::string, Program> programs;
  std::set<std::string> resources;
  std::map<const STY_Style *, const STY_Style *> origins;
  struct Hook {
    const STY_Style *style;
    STY_OverrideScriptHandle script;
    UT_JSONValue original;
  };
  std::map<std::string, Hook> hooks;
  std::map<std::pair<const STY_Style *, Field>, std::string> hook_names;
  std::vector<UT_IntrusivePtr<ScriptValue>> values;
  const STY_Style *origin(const STY_Style &style) const {
    const auto found = origins.find(&style);
    return found == origins.end() ? &style : found->second;
  }
  STY_StyleHandle parse_style(const UT_JSONValue &json, const STY_Style &source) {
    // STY_Style's parser requires its stylesheet's native load context,
    // even when all imported definitions have already been flattened.
    UT_JSONValue document;
    document.startMap();
    auto &styles = document["styles"];
    styles.startArray();
    styles.appendArray(json);
    STY_StyleSheetHandle owner(new STY_StyleSheet(document, source.getSourceLocation().myFile, STY_LOAD_FOR_STYLING));
    retained.push_back(owner);
    if (owner->getStyles().size() != 1)
      throw std::logic_error("Owned stylesheet clone did not retain exactly one native style");
    return owner->getStyles()[0];
  }
  Definition &definition(const STY_Style &style) {
    if (const auto found = origins.find(&style); found != origins.end())
      return definitions.at(found->second);
    auto [found, added] = definitions.try_emplace(&style);
    auto &result = found->second;
    if (!added)
      return result;
    result.origin = origin(style);
    UT_JSONValue json;
    STY_TargetMatchClosureHandle closure;
    style.saveStyleSheets(&json, &closure, 1, style.getTarget(), false, false, true);
    result.flat = parse_style(json, style);
    STY_ResultsFilter all;
    for (const auto &[name, block] : result.flat->getOverrideSets()) {
      UT_StringArray categories;
      block->getMatchingCategories(all, categories);
      for (const auto &category : categories)
        if (supported(category))
          for (const auto &[field, value] : block->getOverrideCategory(category)->getOverrides())
            if (value->getScript())
              result.scripts[{category.toStdString(), field.toStdString()}] = value->getScript();
    }
    scripted |= !result.scripts.empty();
    return result;
  }
  const Program &program(const STY_OverrideScript &script) {
    const auto source = script.getCodeSource(true);
    const auto code = script.getCode(true).toStdString();
    const std::string key = std::to_string(int(source)) + ":" + std::to_string(code.size()) + ":" + code +
                            script.getReturnVariable(true).toStdString();
    auto [found, added] = programs.try_emplace(key);
    auto &result = found->second;
    if (!added)
      return result;
    if (source == STY_OverrideScript::CODE_BINDING)
      return result;
    // Compilation errors belong to this stylesheet, never the SOP which
    // happened to be the last/current Houdini evaluation context.
    UT_AutoErrorManager errors;
    CVEX_Context context;
    CVEX_Function function;
    if (source == STY_OverrideScript::CODE_INLINE) {
      UT_WorkBuffer text;
      text.append(code.c_str());
      function = VCC_Utils::preloadLocalFunction(context, text);
    } else if (source == STY_OverrideScript::CODE_FILE) {
      UT_String command(code.c_str());
      command.expandVariables();
      UT_WorkArgs args;
      command.parse(args);
      if (args.entries()) {
        const std::string name(args[0]);
        result.arguments.clear();
        for (int i = 0; i < args.entries(); ++i)
          result.arguments.emplace_back(args[i]);
        if (name.starts_with("op:"))
          result.cwd = name.substr(3);
        if (name.starts_with("op:") || name.starts_with("opdef:")) {
          resources.insert("vex:" + name);
          refresh_program(name, "vex:" + name);
          std::ostringstream compiled;
          if (VEX_VexResolver::getVexCode(name.c_str(), compiled, VEX_CG_DEFAULT, VEX_CVEX_CONTEXT)) {
            std::istringstream input(compiled.str());
            UT_IStream stream(input);
            function = context.preloadFunction(stream);
          }
        } else if (name.find(':') == std::string::npos || (name.size() > 2 && name[1] == ':')) {
          UT_String resolved;
          const auto *search = UT_PathSearch::getInstance(UT_VEX_CVEX_PATH);
          if (search)
            search->findFile(resolved, name.c_str());
          const auto filename = resolved.isstring() ? resolved.toStdString() : name;
          try {
            std::error_code error;
            const auto path = std::filesystem::absolute(
                std::filesystem::path(std::u8string(filename.begin(), filename.end())), error);
            if (!error) {
              const auto bytes = path.lexically_normal().generic_u8string();
              const auto resource = "file:" + std::string(bytes.begin(), bytes.end());
              resources.insert(resource);
              refresh_program(name, resource);
              std::ifstream input(path, std::ios::binary);
              if (input) {
                UT_IStream stream(input);
                function = context.preloadFunction(stream);
              }
            }
          } catch (const std::filesystem::filesystem_error &) {
            result.error = "Invalid CVEX program filename";
            return result;
          } catch (const std::range_error &) {
            result.error = "Invalid CVEX program filename encoding";
            return result;
          }
        } else {
          result.error = "Unsupported CVEX program source";
          return result;
        }
      }
    } else if (source == STY_OverrideScript::CODE_NODE) {
      const auto path = code.starts_with("op:") ? code : "op:" + code;
      std::ostringstream compiled;
      resources.insert("vex:" + path);
      refresh_program(path, "vex:" + path);
      if (VEX_VexResolver::getVexCode(path.c_str(), compiled, VEX_CG_DEFAULT, VEX_CVEX_CONTEXT)) {
        std::istringstream input(compiled.str());
        UT_IStream stream(input);
        function = context.preloadFunction(stream);
      }
    }
    if (!function.isValid()) {
      result.error = "CVEX compilation or program loading failed";
      if (source == STY_OverrideScript::CODE_FILE || source == STY_OverrideScript::CODE_NODE)
        result.error += ": " + code.substr(0, 512);
      return result;
    }
    UT_StringArray names;
    UT_Array<CVEX_Type> types;
    UT_IntArray exports;
    if (!function.getParameters(names, types, exports)) {
      result.error = "CVEX parameter metadata is unavailable";
      return result;
    }
    int outputs{};
    bool named = false;
    const auto wanted = script.getReturnVariable(true);
    for (exint i = 0; i < names.size(); ++i)
      if (exports[i]) {
        ++outputs;
        named |= names[i] == wanted;
        if (!wanted.isstring() || names[i] == wanted) {
          result.output = names[i];
          result.output_type = types[i];
        }
      }
    if (wanted.isstring() ? !named : outputs != 1) {
      result.error = "CVEX requires one exported result or a valid explicit return name";
      return result;
    }
    if (result.output_type != CVEX_TYPE_INTEGER && result.output_type != CVEX_TYPE_FLOAT &&
        result.output_type != CVEX_TYPE_VECTOR2 && result.output_type != CVEX_TYPE_VECTOR3 &&
        result.output_type != CVEX_TYPE_VECTOR4 && result.output_type != CVEX_TYPE_STRING) {
      result.error = "Unsupported CVEX exported result type";
      return result;
    }
    // Inputs used only by a different export are disconnected from this
    // override. Validate the same reachable program that will execute.
    context.addRequiredOutput(result.output, result.output_type);
    for (exint i = 0; i < names.size(); ++i)
      if (!exports[i])
        context.addInput(names[i], types[i], true);
    std::vector<const char *> arguments;
    for (const auto &value : result.arguments)
      arguments.push_back(value.c_str());
    if (!context.loadFunction(function, int(arguments.size()), arguments.data())) {
      result.error = "CVEX program loading failed";
      return result;
    }
    for (exint i = 0; i < names.size(); ++i)
      if (!exports[i] && context.findInput(names[i], types[i]))
        result.inputs.emplace(names[i].toStdString(), types[i]);
    result.function = function;
    return result;
  }
  STY_BindingDataSourceHandle binding(const STY_Style &style, const STY_Binding &value, const STY_Subject &subject,
                                      bool pre = false) {
    if (value.getDataSourceType() == STY_Binding::SOURCE_CONSTANT)
      return value.getConstantDataSource();
    if (value.getDataSourceType() != STY_Binding::SOURCE_PREBINDING)
      if (auto data = subject.getOverrideBindingDataSource(value.getDataName()))
        return data;
    if (!pre && style.getTarget() && style.getTarget()->getPreBindings())
      for (const auto &candidate : *style.getTarget()->getPreBindings())
        if (candidate->getArgName() == value.getDataName())
          return binding(style, *candidate, subject, true);
    return {};
  }
  STY_BindingDataSourceHandle script_value(const Hook &hook, const STY_Subject &subject) {
    auto value = UT_IntrusivePtr<ScriptValue>(new ScriptValue);
    value->program = &program(*hook.script);
    const auto &compiled = *value->program;
    value->result = Buffer(compiled.output_type, 1).get(0);
    std::string location = hook.style->getSourceLocation().myFile.toStdString();
    if (!compiled.cwd.empty())
      location = compiled.cwd;
    if (hook.script->getCodeSource(true) == STY_OverrideScript::CODE_NODE)
      location = hook.script->getCode(true).toStdString();
    if (location.starts_with("op:"))
      location = location.substr(3);
    auto *node = location.starts_with('/') ? OPgetDirector()->findNode(location.c_str()) : nullptr;
    if (!node && !cwd.empty())
      node = OPgetDirector()->findNode(cwd.c_str());
    value->cwd = node ? node->getUniqueId() : -1;
    if (const auto *bindings = hook.script->getBindings(true))
      for (const auto &bound : *bindings) {
        const auto found = compiled.inputs.find(bound->getArgName().toStdString());
        if (found == compiled.inputs.end())
          continue;
        auto owned = read_value(binding(*hook.style, *bound, subject));
        if (owned.type != CVEX_TYPE_INVALID)
          value->inputs.emplace(found->first, std::move(owned));
      }
    values.push_back(value);
    return STY_BindingDataSourceHandle(value.get());
  }
  bool run_pending() {
    using Key = std::tuple<const Program *, int, std::vector<std::string>>;
    std::map<Key, std::vector<ScriptValue *>> batches;
    for (const auto &value : values)
      if (value->needed && !value->complete) {
        std::vector<std::string> names;
        for (const auto &[name, input] : value->inputs)
          names.push_back(name);
        batches[{value->program, value->cwd, std::move(names)}].push_back(value.get());
      }
    for (const auto &[key, batch] : batches) {
      const auto *program = std::get<0>(key);
      for (std::size_t first = 0; first < batch.size(); first += 4096) {
        const int count = int(std::min<std::size_t>(4096, batch.size() - first));
        CVEX_Context context;
        UT_AutoErrorManager errors;
        std::map<std::string, Buffer> inputs;
        for (const auto &name : std::get<2>(key)) {
          const auto type = program->inputs.at(name);
          context.addInput(name.c_str(), type, true);
          auto &buffer = inputs.try_emplace(name, type, count).first->second;
          for (int i = 0; i < count; ++i)
            buffer.set(i, batch[first + i]->inputs.at(name));
        }
        context.addRequiredOutput(program->output, program->output_type);
        std::vector<const char *> args;
        for (const auto &value : program->arguments)
          args.push_back(value.c_str());
        std::string reason;
        if (!context.loadFunction(program->function, int(args.size()), args.data()))
          reason = "CVEX program loading failed";
        if (reason.empty())
          for (auto &[name, buffer] : inputs) {
            auto *input = context.findInput(name.c_str());
            if (input && !buffer.bind(*input)) {
              reason = "CVEX input buffer binding failed";
              break;
            }
          }
        Buffer output(program->output_type, count);
        auto *result = context.findOutput(program->output, program->output_type);
        if (reason.empty() && (!result || !output.bind(*result)))
          reason = "CVEX result buffer binding failed";
        struct Caller final : UT_OpCaller {
          int cwd;
          std::set<int> ids;
          explicit Caller(int cwd) : cwd(cwd) {}
          int getOpId() override { return cwd; }
          void referenceOtherOp(int id, UT_OpCallee *cache, bool, bool = false) override {
            ids.insert(id);
            if (cache)
              notifyCalleeWhenDone(cache);
          }
          void referenceOtherParameter(int id, int, int, bool = true) override { ids.insert(id); }
        } caller(std::get<1>(key));
        CVEX_RunData data;
        data.setTime(time);
        data.setCWDNodeId(caller.cwd);
        data.setOpCaller(&caller);
        if (reason.empty() && !context.run(count, true, &data))
          reason = "CVEX execution failed";
        for (auto id : caller.ids)
          if (auto *node = OP_Node::lookupNode(id)) {
            UT_String path;
            node->getFullPath(path);
            resources.insert("op:" + path.toStdString());
          }
        for (int i = 0; i < count; ++i) {
          auto &value = *batch[first + i];
          value.error = reason;
          value.complete = true;
          if (reason.empty())
            value.result = output.get(result->isVarying() ? i : 0);
        }
      }
    }
    return !batches.empty();
  }
  bool evaluate_values(const STY_Results &results) {
    // STY stores lazy option entries. Import once to discover only the
    // selected binding reads, execute their batches, then ask STY for
    // fresh results. A placeholder must never reach scene translation.
    for (const auto &[category, fields] : results)
      for (const auto &[name, value] : fields)
        usable(value.myValue);
    return run_pending();
  }
  bool evaluate_values(const UT_Array<STY_Results> &results) {
    for (const auto &value : results)
      for (const auto &[category, fields] : value)
        for (const auto &[name, field] : fields)
          usable(field.myValue);
    return run_pending();
  }
  std::string save(const STY_Styler &styler) {
    UT_StringHolder text;
    styler.saveStyleSheet(text);
    if (run_pending())
      styler.saveStyleSheet(text);
    UT_JSONValue document;
    if (!document.parseValue(text))
      throw std::logic_error("Native stylesheet serialization is invalid JSON");
    std::function<void(UT_JSONValue &)> restore = [&](UT_JSONValue &value) {
      if (value.isMap()) {
        auto script = value.find("script");
        if (!script.atEnd() && script->isMap()) {
          auto bound = script->find("binding");
          if (!bound.atEnd())
            if (const auto found = hooks.find(bound->getString().toStdString()); found != hooks.end()) {
              value = found->second.original;
              return;
            }
        }
      }
      if (value.isMap() || value.isArray())
        for (auto &child : value)
          restore(child);
    };
    restore(document);
    UT_WorkBuffer result;
    {
      UT_AutoJSONWriter writer(result);
      document.save(writer);
    }
    return result.buffer();
  }
  std::string binding_error(const STY_Style &style, const STY_OverrideScript &script, const STY_Subject &subject) {
    const auto &compiled = program(script);
    if (!compiled.error.empty())
      return compiled.error;
    struct Resolved {
      STY_BindingDataSourceHandle source;
      bool constant{};
    };
    std::function<Resolved(const STY_Binding &, bool)> resolve;
    resolve = [&](const STY_Binding &binding, bool pre) -> Resolved {
      if (binding.getDataSourceType() == STY_Binding::SOURCE_CONSTANT)
        return {binding.getConstantDataSource(), true};
      const auto data_name = binding.getDataName();
      if (binding.getDataSourceType() != STY_Binding::SOURCE_PREBINDING)
        if (auto data = subject.getOverrideBindingDataSource(data_name))
          return {data, false};
      if (!pre && style.getTarget() && style.getTarget()->getPreBindings())
        for (const auto &candidate : *style.getTarget()->getPreBindings())
          if (candidate->getArgName() == data_name)
            return resolve(*candidate, true);
      return {};
    };
    if (const auto *bindings = script.getBindings(true))
      for (const auto &binding : *bindings) {
        const auto expected = compiled.inputs.find(binding->getArgName().toStdString());
        if (script.getCodeSource(true) != STY_OverrideScript::CODE_BINDING && expected == compiled.inputs.end())
          continue;
        STY_BindingDataInfo info;
        const auto data = resolve(*binding, false);
        if (!finite(data.source, info))
          return "Missing, non-finite or unsupported CVEX binding: " + binding->getDataName().toStdString();
        // Native constant bindings promote an integer to a scalar float;
        // stylesheet serialization also writes integral floats as integers.
        const bool promoted = data.constant && type(info) == CVEX_TYPE_INTEGER && expected != compiled.inputs.end() &&
                              expected->second == CVEX_TYPE_FLOAT;
        if (expected != compiled.inputs.end() && type(info) != expected->second && !promoted)
          return "CVEX binding type does not match argument " + binding->getArgName().toStdString() + ": " +
                 binding->getDataName().toStdString();
      }
    else if (script.getCodeSource(true) == STY_OverrideScript::CODE_BINDING) {
      STY_BindingDataInfo info;
      const auto data = subject.getOverrideBindingDataSource(script.getCode(true));
      if (!finite(data, info))
        return "Missing, non-finite or unsupported CVEX binding: " + script.getCode(true).toStdString();
    }
    return {};
  }
  STY_StyleSheetHandle clone(const STY_StyleSheetHandle &sheet, const Rejections *rejected = nullptr) {
    if (!sheet)
      return {};
    if (!rejected)
      if (const auto found = sheets.find(sheet.get()); found != sheets.end())
        return found->second;
    // Retain original source identities as well as owned clones. Subjects
    // may return temporary stylesheet handles; pointer reuse must never
    // alias a later component's definition or recovery mask.
    retained.push_back(sheet);
    UT_Array<STY_StyleHandle> styles;
    styles.setCapacity(sheet->getStyles().size());
    for (const auto &style : sheet->getStyles()) {
      auto &meta = definition(*style);
      UT_JSONValue json;
      STY_TargetMatchClosureHandle closure;
      style->saveStyleSheets(&json, &closure, 1, style->getTarget(), false, false, true);
      auto &overrides = map(json, "overrides");
      map(overrides, stylesheet_recovery_category);
      STY_ResultsFilter all;
      for (const auto &[name, block] : meta.flat->getOverrideSets()) {
        UT_StringArray categories;
        block->getMatchingCategories(all, categories);
        for (const auto &category : categories)
          if (!supported(category) && category != stylesheet_recovery_category)
            for (const auto &[field, value] : block->getOverrideCategory(category)->getOverrides())
              if (value->getScript()) {
                remove(map(overrides, category.toStdString()), field.toStdString());
                if (map(overrides, category.toStdString()).count() == 0)
                  remove(overrides, category.toStdString());
                map(overrides,
                    stylesheet_recovery_category)[marker({category.toStdString(), field.toStdString()}).c_str()] =
                    UT_StringHolder("Unsupported scripted stylesheet category: " + category.toStdString() + "." +
                                    field.toStdString());
              }
        for (const auto &category : categories)
          if (supported(category))
            for (const auto &[field, value] : block->getOverrideCategory(category)->getOverrides()) {
              const Field key{category.toStdString(), field.toStdString()};
              std::string reason;
              if (value->getScript())
                reason = program(*value->getScript()).error;
              if (rejected)
                if (const auto found = rejected->find(meta.origin); found != rejected->end())
                  if (const auto bad = found->second.find(key); bad != found->second.end())
                    reason = bad->second;
              const auto marker_name = marker(key);
              const bool existing = !map(overrides, stylesheet_recovery_category).find(marker_name.c_str()).atEnd();
              if (!reason.empty()) {
                remove(map(overrides, key.first), key.second);
                if (map(overrides, key.first).count() == 0)
                  remove(overrides, key.first);
                map(overrides, stylesheet_recovery_category)[marker_name.c_str()] = UT_StringHolder(
                    key.first + "." + key.second + ": " + reason + " [" + style->getPath().toStdString() + "]");
              } else {
                if (value->getScript() && value->getScript()->getCodeSource(true) != STY_OverrideScript::CODE_BINDING) {
                  auto &entry = map(overrides, key.first)[key.second.c_str()];
                  auto [found, added] = hook_names.try_emplace({meta.origin, key});
                  if (added) {
                    found->second = "__cycles_stylesheet_" + std::to_string(reinterpret_cast<std::uintptr_t>(this)) +
                                    "_" + std::to_string(hooks.size());
                    hooks.emplace(found->second, Hook{meta.flat.get(), value->getScript(), entry});
                  }
                  entry.startMap();
                  map(entry, "script")["binding"] = UT_StringHolder(found->second);
                }
                if (!existing)
                  map(overrides, stylesheet_recovery_category)[marker_name.c_str()] = UT_StringHolder("");
              }
            }
      }
      auto owned = parse_style(json, *style);
      origins[owned.get()] = meta.origin;
      styles.append(owned);
    }
    STY_StyleSheetHandle result(new STY_StyleSheet(UT_StringArray{}, styles, sheet->getMaterialDefinitions(),
                                                   sheet->getScripts(), sheet->getOverrideSets(), sheet->getFlags()));
    retained.push_back(result);
    if (!rejected)
      sheets.emplace(sheet.get(), result);
    return result;
  }
  STY_Styler parent(const std::vector<STY_StyleSheetHandle> &layers, const Rejections *rejected = nullptr) {
    STY_Styler root;
    for (const auto &sheet : layers)
      if (auto owned = clone(sheet, rejected))
        root = root.cloneWithAddedStyleSheet(owned);
    return root;
  }
  struct Subject final : STY_Subject {
    Impl &owner;
    const STY_Subject &source;
    const Rejections *rejected;
    mutable std::map<std::string, STY_BindingDataSourceHandle> data;
    Subject(Impl &owner, const STY_Subject &source, const Rejections *rejected = nullptr)
        : owner(owner), source(source), rejected(rejected) {}
    STY_TargetMatchStatus matchesStyleTarget(const STY_TargetHandle &target) const override {
      return source.matchesStyleTarget(target);
    }
    STY_BindingDataSourceHandle getOverrideBindingDataSource(const UT_StringHolder &name) const override {
      const auto key = name.toStdString();
      if (const auto hook = owner.hooks.find(key); hook != owner.hooks.end()) {
        const auto found = data.find(key);
        if (found != data.end())
          return found->second;
        return data.emplace(key, owner.script_value(hook->second, source)).first->second;
      }
      return source.getOverrideBindingDataSource(name);
    }
    STY_StyleSheetHandle getPreFilterStyleSheet() const override {
      return owner.clone(source.getPreFilterStyleSheet(), rejected);
    }
    STY_StyleSheetHandle getPostFilterStyleSheet() const override {
      return owner.clone(source.getPostFilterStyleSheet(), rejected);
    }
    UT_StringHolder getSourceLocation() const override { return source.getSourceLocation(); }
  };
  struct Subjects final : STY_SubjectGroup {
    std::deque<Subject> values;
    Subjects(Impl &owner, const STY_SubjectGroup &source) {
      for (int i = 0; i < source.getNumSubjects(); ++i)
        values.emplace_back(owner, source.getSubject(i));
    }
    Subjects(Impl &owner, const STY_SubjectGroup &source, const std::vector<int> &indices, const Rejections *rejected) {
      for (auto i : indices)
        values.emplace_back(owner, source.getSubject(i), rejected);
    }
    int getNumSubjects() const override { return int(values.size()); }
    const STY_Subject &getSubject(int index) const override { return values.at(std::size_t(index)); }
  };
  STY_Styler context_parent(const std::vector<STY_StyleSheetHandle> &layers, const Rejections *rejected,
                            const STY_Subject *ancestor) {
    auto root = parent(layers, rejected);
    if (!ancestor)
      return root;
    Subject owned(*this, *ancestor, rejected);
    // The containing primitive is the native parent. Its full matches keep
    // precedence, while pending subtargets advance to actual source keys.
    // Independent root point targets were pruned by the primitive subject;
    // restore only pending candidates as lower-priority DOM fallbacks.
    // Already-full root/self rules occur once in this composed hierarchy.
    return root.cloneWithSubject(owned).cloneWithAddedStyler(root.cloneWithOverridesRemoved(nullptr, true));
  }
  bool reject_bindings(const STY_Styler &styler, const STY_Subject &subject, Rejections &rejected) {
    CollectStyles filter;
    STY_Results unused;
    styler.getResults(unused, filter);
    bool changed = false;
    for (const auto *style : filter.styles) {
      auto &meta = definition(*style);
      for (const auto &[field, script] : meta.scripts) {
        const auto reason = binding_error(*meta.flat, *script, subject);
        if (!reason.empty())
          changed |= rejected[meta.origin].emplace(field, reason).second;
      }
    }
    return changed;
  }
  bool reject_results(const STY_Results &results, Rejections &rejected) {
    bool changed = false;
    const auto markers = results.find(stylesheet_recovery_category);
    if (markers == results.end())
      return false;
    for (const auto &[name, value] : markers->second) {
      if (!value.mySource)
        continue;
      UT_StringHolder diagnostic;
      if (!value.myValue || !value.myValue->importOption(diagnostic) || diagnostic.isstring())
        continue;
      auto &meta = definition(*value.mySource);
      for (const auto &[field, script] : meta.scripts)
        if (marker(field) == name.toStdString()) {
          const auto category = results.find(field.first.c_str());
          bool valid = false;
          if (category != results.end()) {
            const auto output = category->second.find(field.second.c_str());
            valid = output != category->second.end() && output->second.mySource == value.mySource &&
                    usable(output->second.myValue);
          }
          if (!valid)
            changed |= rejected[meta.origin].emplace(field, "CVEX did not produce a usable finite result").second;
        }
    }
    return changed;
  }
  void reject_remaining(const STY_Styler &styler, Rejections &rejected) {
    CollectStyles filter;
    STY_Results unused;
    styler.getResults(unused, filter);
    for (const auto *style : filter.styles) {
      auto &meta = definition(*style);
      for (const auto &[field, script] : meta.scripts)
        rejected[meta.origin].try_emplace(
            field, "CVEX recovery budget reached; omitting remaining scripted fields for this component");
    }
  }
  STY_Styler rebuild(const std::vector<STY_StyleSheetHandle> &layers, const STY_Subject &source,
                     const Rejections &rejected, const STY_Subject *ancestor = nullptr) {
    Subject subject(*this, source, &rejected);
    return context_parent(layers, &rejected, ancestor).cloneWithSubject(subject);
  }
};
NativeStyleEvaluation::NativeStyleEvaluation(double time, const std::string &cwd)
    : impl_(std::make_unique<Impl>(time, cwd)) {}
NativeStyleEvaluation::~NativeStyleEvaluation() = default;
STY_StyleSheetHandle NativeStyleEvaluation::prepare(const STY_StyleSheetHandle &sheet) { return impl_->clone(sheet); }
bool NativeStyleEvaluation::scripted() const { return impl_->scripted; }
std::vector<std::string> NativeStyleEvaluation::resources() const {
  return {impl_->resources.begin(), impl_->resources.end()};
}
std::string NativeStyleEvaluation::save(const STY_Styler &styler) { return impl_->save(styler); }
STY_Styler NativeStyleEvaluation::object(const std::vector<STY_StyleSheetHandle> &layers, const STY_Subject &source) {
  Impl::Subject subject(*impl_, source);
  auto result = impl_->parent(layers).cloneWithSubject(subject);
  Rejections rejected;
  if (impl_->reject_bindings(result, source, rejected))
    result = impl_->rebuild(layers, source, rejected);
  for (int pass = 0; pass <= 32; ++pass) {
    if (pass == 32) {
      impl_->reject_remaining(result, rejected);
      result = impl_->rebuild(layers, source, rejected);
    }
    STY_Results values;
    STY_MaterialResultsFilter filter;
    UT_AutoErrorManager errors;
    result.getResults(values, filter);
    if (impl_->evaluate_values(values)) {
      values.clear();
      result.getResults(values, filter);
    }
    if (!impl_->reject_results(values, rejected))
      return result;
    result = impl_->rebuild(layers, source, rejected);
  }
  throw std::logic_error("Stylesheet script recovery exceeded the finite field-removal contract");
}
std::vector<STY_Styler> NativeStyleEvaluation::evaluate(const std::vector<STY_StyleSheetHandle> &layers,
                                                        const STY_SubjectGroup &source, UT_Array<STY_Results> &results,
                                                        const STY_Subject *ancestor) {
  Impl::Subjects subjects(*impl_, source);
  STY_StylerGroup initial;
  initial.append(impl_->context_parent(layers, nullptr, ancestor), subjects);
  std::vector<STY_Styler> stylers;
  std::vector<Rejections> rejected(std::size_t(source.getNumSubjects()));
  for (int i = 0; i < initial.getNumStylers(); ++i) {
    auto value = initial.getStyler(i);
    auto &bad = rejected[std::size_t(i)];
    if (impl_->reject_bindings(value, source.getSubject(i), bad))
      value = impl_->rebuild(layers, source.getSubject(i), bad, ancestor);
    stylers.push_back(std::move(value));
  }
  for (int pass = 0; pass <= 32; ++pass) {
    if (pass == 32)
      for (int i = 0; i < source.getNumSubjects(); ++i)
        impl_->reject_remaining(stylers[std::size_t(i)], rejected[std::size_t(i)]);
    STY_MaterialResultsFilter filter;
    UT_AutoErrorManager errors;
    if (pass == 0 && std::all_of(rejected.begin(), rejected.end(), [](const auto &mask) { return mask.empty(); })) {
      initial.getResults(results, filter);
      if (impl_->evaluate_values(results)) {
        results.clear();
        initial.getResults(results, filter);
      }
    } else {
      // Rebuild native subject batches, not a batch of extracted scalar
      // stylers: the latter retains group-relative closure indices.
      std::map<Rejections, std::vector<int>> batches;
      for (int i = 0; i < source.getNumSubjects(); ++i)
        batches[rejected[std::size_t(i)]].push_back(i);
      results.clear();
      results.setSize(source.getNumSubjects());
      for (const auto &[mask, indices] : batches) {
        const auto *bad = mask.empty() ? nullptr : &mask;
        Impl::Subjects selected(*impl_, source, indices, bad);
        STY_StylerGroup group;
        group.append(impl_->context_parent(layers, bad, ancestor), selected);
        UT_Array<STY_Results> values;
        group.getResults(values, filter);
        if (impl_->evaluate_values(values)) {
          values.clear();
          group.getResults(values, filter);
        }
        for (std::size_t i = 0; i < indices.size(); ++i) {
          results[indices[i]] = values[exint(i)];
          stylers[std::size_t(indices[i])] = group.getStyler(int(i));
        }
      }
    }
    bool changed = false;
    for (int i = 0; i < source.getNumSubjects(); ++i)
      if (impl_->reject_results(results[i], rejected[std::size_t(i)])) {
        stylers[std::size_t(i)] = impl_->rebuild(layers, source.getSubject(i), rejected[std::size_t(i)], ancestor);
        changed = true;
      }
    if (!changed)
      return stylers;
  }
  throw std::logic_error("Stylesheet script recovery exceeded the finite field-removal contract");
}
} // namespace cycles_houdini
