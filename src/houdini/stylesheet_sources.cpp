// SPDX-License-Identifier: Apache-2.0
#include "stylesheet_sources.h"
#include "stylesheet_evaluation.h"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <set>
#include <stdexcept>
#include <CH/CH_Manager.h>
#include <OP/OP_Director.h>
#include <OP/OP_StyleManager.h>
#include <OP/OP_Bundle.h>
#include <OP/OP_BundleList.h>
#include <GSTY/GSTY_SubjectGeoObject.h>
#include <SOP/SOP_BundleMap.h>
#include <STY/STY_Styler.h>
#include <UT/UT_JSONValue.h>
#include <UT/UT_String.h>
#include <UT/UT_Thread.h>
#include <UT/UT_ErrorManager.h>
#include <VEX/VEX_VexResolver.h>

namespace cycles_houdini {
namespace {
constexpr std::size_t source_limit = 1024 * 1024, total_limit = 8 * source_limit, document_limit = 256;
constexpr int depth_limit = 32;
OP_StyleManager &manager() {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Stylesheet sources require host acquisition");
  auto *director = OPgetDirector();
  auto *result = director ? director->getStyleManager() : nullptr;
  if (!result)
    throw std::logic_error("Houdini stylesheet manager is unavailable");
  return *result;
}
std::string utf8(const std::filesystem::path &path) {
  auto value = path.generic_u8string();
  return {value.begin(), value.end()};
}
std::filesystem::path file_path(const std::string &value) {
  return std::filesystem::path(std::u8string(value.begin(), value.end()));
}
} // namespace
std::vector<std::string> stylesheet_source_signature(const std::vector<std::string> &resources) {
  auto &styles = manager();
  std::vector<std::string> result;
  result.reserve(resources.size());
  for (const auto &resource : resources) {
    if (resource.starts_with("op:")) {
      auto *node = OPgetDirector()->findNode(resource.substr(3).c_str());
      result.push_back(node ? std::to_string(node->getUniqueId()) + ":" +
                                  std::to_string(std::int64_t(node->getVersionParms()))
                            : "missing");
      continue;
    }
    if (resource.starts_with("vex:")) {
      UT_AutoErrorManager errors;
      result.push_back(std::to_string(VEX_VexResolver::getCodeTimeStamp(resource.substr(4).c_str(), VEX_CVEX_CONTEXT)));
      continue;
    }
    if (resource.starts_with("style:")) {
      result.push_back(std::to_string(styles.getStyleVersion(resource.substr(6).c_str())));
      continue;
    }
    if (resource.starts_with("bundle:")) {
      auto *bundles = OPgetDirector()->getBundles();
      auto *bundle = bundles ? bundles->getBundle(resource.substr(7).c_str()) : nullptr;
      if (!bundle) {
        result.push_back("missing");
        continue;
      }
      std::vector<int> members;
      for (int i = 0; i < bundle->entries(); ++i)
        if (auto *node = bundle->getNode(i))
          members.push_back(node->getUniqueId());
      std::sort(members.begin(), members.end());
      std::string value = "members:";
      for (auto id : members)
        value += std::to_string(id) + ",";
      result.push_back(std::move(value));
      continue;
    }
    if (!resource.starts_with("file:"))
      throw std::invalid_argument("Invalid internal stylesheet resource identity");
    const auto path = file_path(resource.substr(5));
    std::error_code ec;
    const auto status = std::filesystem::status(path, ec);
    if (ec || !std::filesystem::is_regular_file(status)) {
      result.push_back("missing:" + std::to_string(ec.value()) + ":" + std::to_string(int(status.type())));
      continue;
    }
    const auto size = std::filesystem::file_size(path, ec);
    if (ec) {
      result.push_back("unreadable:" + std::to_string(ec.value()));
      continue;
    }
    const auto modified = std::filesystem::last_write_time(path, ec);
    result.push_back(ec ? "unreadable:" + std::to_string(ec.value())
                        : std::to_string(size) + ":" + std::to_string(modified.time_since_epoch().count()));
  }
  return result;
}
NativeStyleSources stylesheet_sources(const std::string &root) {
  auto &styles = manager();
  NativeStyleSources result;
  std::set<std::string> resources, visited, ancestors;
  std::size_t total{}, documents{};
  const auto problem = [&](const std::string &source, const std::string &reason, bool unsafe = false) {
    result.errors.push_back(source + ": " + reason);
    result.safe &= !unsafe;
  };
  std::function<void(const std::string &, const std::string &, int)> visit;
  visit = [&](const std::string &text, const std::string &origin, int depth) {
    if (text.size() > source_limit || total + text.size() > total_limit || ++documents > document_limit ||
        depth > depth_limit || text.find('\0') != std::string::npos) {
      problem(origin,
              "Stylesheet exceeds the 1 MiB/source, 8 MiB/import graph, 256-document or 32-level acquisition budget, "
              "or contains NULs; omitting this stylesheet layer",
              true);
      return;
    }
    total += text.size();
    UT_JSONValue value;
    if (!value.parseValue(UT_StringHolder(text)) || !value.isMap()) {
      problem(origin, "Invalid stylesheet JSON; keeping usable rules from other layers");
      return;
    }
    std::function<void(const UT_JSONValue &)> scripts;
    scripts = [&](const UT_JSONValue &item) {
      if (item.isMap())
        for (const auto &[name, child] : item.items()) {
          if (name == "script" && child.isMap())
            result.scripted = true;
          if (child.isMap() || child.isArray())
            scripts(child);
        }
      else if (item.isArray())
        for (const auto &child : item)
          scripts(child);
    };
    scripts(value);
    std::function<void(const UT_JSONValue &)> target_dependencies;
    target_dependencies = [&](const UT_JSONValue &target) {
      if (!target.isMap())
        return;
      const auto bundle = target.find("objectBundle");
      if (!bundle.atEnd() && bundle->getType() == UT_JSONValue::JSON_STRING)
        resources.insert("bundle:" + bundle->getString().toStdString());
      const auto sub = target.find("subTarget");
      if (!sub.atEnd())
        target_dependencies(*sub);
    };
    const auto rules = value.find("styles");
    if (!rules.atEnd() && rules->isArray())
      for (const auto &rule : *rules)
        if (rule.isMap()) {
          const auto target = rule.find("target");
          if (!target.atEnd())
            target_dependencies(*target);
        }
    auto imports = value.find("importFiles");
    if (imports.atEnd())
      return;
    if (!imports->isArray()) {
      problem(origin, "importFiles must be an array; omitting this stylesheet layer", true);
      return;
    }
    for (const auto &item : *imports) {
      if (item.getType() != UT_JSONValue::JSON_STRING) {
        problem(origin, "importFiles contains a non-string source; omitting this stylesheet layer", true);
        continue;
      }
      const auto name = item.getString().toStdString();
      if (name.empty() || name.find('\0') != std::string::npos) {
        problem(origin, "Empty or invalid stylesheet import; omitting this stylesheet layer", true);
        continue;
      }
      // The native evaluator resolves registered names before filenames.
      // Watch that name even when absent so creation/deletion repairs IPR.
      resources.insert("style:" + name);
      std::string identity, text;
      if (styles.hasStyle(name.c_str())) {
        identity = "style:" + name;
        text = styles.getStyleSheet(name.c_str()).toStdString();
      } else {
        UT_String expanded(name.c_str());
        expanded.expandVariables();
        const std::string path_text = expanded.toStdString();
        // Houdini's native import syntax is a registered name or file.
        // Do not open unsupported URI streams during renderer capture.
        const auto colon = path_text.find(':');
        const bool drive =
            path_text.size() >= 3 && colon == 1 &&
            ((path_text[0] >= 'A' && path_text[0] <= 'Z') || (path_text[0] >= 'a' && path_text[0] <= 'z'));
        if (colon != std::string::npos && !drive) {
          problem(origin, "Unsupported stylesheet import source: " + name + "; omitting this stylesheet layer", true);
          continue;
        }
        std::error_code ec;
        std::filesystem::path path;
        try {
          path = std::filesystem::absolute(file_path(path_text), ec).lexically_normal();
        } catch (const std::filesystem::filesystem_error &) {
          problem(origin, "Invalid stylesheet filename: " + name, true);
          continue;
        } catch (const std::range_error &) {
          problem(origin, "Invalid stylesheet filename encoding: " + name, true);
          continue;
        }
        if (ec) {
          problem(origin, "Cannot resolve stylesheet import: " + name, true);
          continue;
        }
        identity = "file:" + utf8(path);
        resources.insert(identity);
        if (visited.contains(identity))
          continue;
        const auto size = std::filesystem::file_size(path, ec);
        if (ec) {
          problem(identity, "Missing or unreadable stylesheet import; keeping supported rules and underlying bindings");
          visited.insert(identity);
          continue;
        }
        if (size > source_limit || size > total_limit - total) {
          problem(identity, "Imported stylesheet exceeds the source budget; omitting this stylesheet layer", true);
          visited.insert(identity);
          continue;
        }
        std::ifstream stream(path, std::ios::binary);
        if (!stream) {
          problem(identity, "Unreadable stylesheet import; keeping supported rules and underlying bindings");
          visited.insert(identity);
          continue;
        }
        text.resize(std::size_t(size));
        stream.read(text.data(), std::streamsize(size));
        if (!stream && std::size_t(stream.gcount()) != size) {
          problem(identity, "Stylesheet import changed while reading; keeping supported rules and underlying bindings");
          visited.insert(identity);
          continue;
        }
      }
      if (ancestors.contains(identity)) {
        problem(identity,
                "Cyclic stylesheet import; native Houdini omits the recursive import and retains valid rules");
        continue;
      }
      if (!visited.insert(identity).second)
        continue;
      ancestors.insert(identity);
      visit(text, identity, depth + 1);
      ancestors.erase(identity);
    }
  };
  visit(root, "material_stylesheet", 0);
  result.resources.assign(resources.begin(), resources.end());
  return result;
}
NativeObjectStyles object_stylesheet(const std::string &path, const std::vector<std::string> &layers,
                                     const std::string &own, const std::string &categories, double time) {
  manager();
  auto *object = OPgetDirector()->findNode(path.c_str());
  CH_AutoEvaluateTime evaluation_time(*CH_Manager::getContext(), SYSgetSTID(), time);
  UT_AutoErrorManager source_errors;
  if (!object)
    throw std::invalid_argument("Stylesheet object no longer exists: " + path);
  NativeObjectStyles result;
  std::vector<STY_StyleSheetHandle> layers_owned;
  std::set<std::string> resources;
  const auto load = [&](const std::string &text) -> STY_StyleSheetHandle {
    if (text.empty() || text == "{}")
      return {};
    auto info = stylesheet_sources(text);
    resources.insert(info.resources.begin(), info.resources.end());
    result.scripted |= info.scripted;
    result.errors.insert(result.errors.end(), info.errors.begin(), info.errors.end());
    if (!info.safe)
      return {};
    STY_StyleSheetHandle sheet(new STY_StyleSheet(text.c_str(), UT_StringHolder(path), STY_LOAD_FOR_STYLING));
    if (sheet->getErrors().length())
      result.errors.emplace_back(sheet->getErrors().buffer());
    return sheet;
  };
  // Callers supply layers from highest to lowest priority. Object-owned
  // rules are appended by the subject, below scene rules, just as in HOM.
  for (const auto &text : layers)
    if (auto sheet = load(text))
      layers_owned.push_back(sheet);
  const auto own_sheet = load(own);
  UT_WorkBuffer object_json;
  if (own_sheet)
    own_sheet->saveStyleSheet(object_json);
  UT_TagManager tag_manager;
  UT_String errors;
  auto tags = tag_manager.createList(categories.c_str(), errors);
  if (errors.length())
    result.errors.emplace_back(errors.toStdString());
  SOP_BundleMap bundles;
  bundles.add(object);
  GSTY_SubjectGeoObject subject(UT_StringHolder(path), tags, bundles, UT_StringHolder(object_json.buffer()));
  NativeStyleEvaluation evaluation(time, path);
  STY_Styler filtered;
  if (result.scripted)
    filtered = evaluation.object(layers_owned, subject);
  else {
    for (const auto &sheet : layers_owned)
      filtered = filtered.cloneWithAddedStyleSheet(sheet);
    filtered = filtered.cloneWithSubject(subject);
  }
  UT_WorkBuffer matching_errors;
  filtered.getErrors(matching_errors);
  if (matching_errors.length())
    result.errors.emplace_back(matching_errors.buffer());
  if (filtered.hasStyles()) {
    if (result.scripted)
      result.json = evaluation.save(filtered);
    else {
      UT_StringHolder json;
      filtered.saveStyleSheet(json);
      result.json = json.toStdString();
    }
  }
  for (const auto &resource : evaluation.resources())
    resources.insert(resource);
  result.resources.assign(resources.begin(), resources.end());
  return result;
}
} // namespace cycles_houdini
