// SPDX-License-Identifier: Apache-2.0
#include "material_stylesheets.h"
#include "scene_adapter.h"
#include "stylesheet_evaluation.h"
#include <algorithm>
#include <limits>
#include <cmath>
#include <bit>
#include <chrono>
#include <iomanip>
#include <functional>
#include <sstream>
#include <stdexcept>
#include <CH/CH_Manager.h>
#include <GA/GA_Iterator.h>
#include <GEO/GEO_PrimPart.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimPacked.h>
#include <GSTY/GSTY_SubjectPrimGroup.h>
#include <GSTY/GSTY_SubjectPointGroup.h>
#include <STY/STY_SubjectGroup.h>
#include <STY/STY_Subject.h>
#include <STY/STY_TargetMatchStatus.h>
#include <STY/STY_Results.h>
#include <STY/STY_ResultsFilter.h>
#include <STY/STY_Style.h>
#include <STY/STY_StylerGroup.h>
#include <UT/UT_OptionEntry.h>
#include <UT/UT_Thread.h>
#include <UT/UT_ErrorManager.h>
#include <UT/UT_WorkBuffer.h>
#include <UT/UT_XXHash.h>

namespace cycles_houdini {
std::uint32_t NativeMaterialStyles::Layers::intern(Layer value) {
  if (sealed_)
    throw std::logic_error("Cannot change frozen native stylesheet layers");
  if (const auto found = interned_.find(value); found != interned_.end())
    return found->second;
  if (values_.size() >= std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error("Native stylesheet layer table exceeds index limits");
  const auto slot = std::uint32_t(values_.size());
  values_.push_back(std::move(value));
  interned_.emplace(values_.back(), slot);
  return slot;
}
void NativeMaterialStyles::Layers::seal() {
  if (sealed_)
    throw std::logic_error("Native stylesheet layers were frozen twice");
  if (values_.size() == 1 && slots_.size() > 1)
    std::vector<std::uint32_t>{0}.swap(slots_);
  interned_.clear();
  sealed_ = true;
}
GeometryRequest sample_object_materials(const GeometryRequest &request, double time) {
  if (request.object_material_samples.empty())
    return request;
  const auto found =
      std::min_element(request.object_material_samples.begin(), request.object_material_samples.end(),
                       [&](const auto &a, const auto &b) { return std::abs(a.time - time) < std::abs(b.time - time); });
  if (std::abs(found->time - time) > 4 * std::numeric_limits<double>::epsilon() * std::max(1., std::abs(time)))
    throw std::logic_error("Owned object material samples do not cover the shutter time");
  auto result = request;
  result.set_material_assignment(found->assignment);
  result.stylesheet = found->stylesheet;
  result.stylesheet_errors = found->errors;
  result.stylesheet_resources = found->resources;
  result.native_styles.reset();
  result.native_materials.reset();
  result.native_material_policy.reset();
  result.native_fallback.reset();
  return result;
}
void object_material_identifiers(std::vector<std::int64_t> &values, const GeometryRequest &request) {
  std::string text;
  const auto add = [&](const std::string &value) { text += std::to_string(value.size()) + ":" + value; };
  const auto assignment = [&](const NativeMaterialAssignment &value) {
    // This function also runs under converter ownership. Use only already
    // frozen identities; never resolve a live OP from a geometry fingerprint.
    const auto found =
        request.native_materials ? request.native_materials->find(value) : NativeMaterialLookup::const_iterator{};
    add(request.native_materials && found != request.native_materials->end() ? found->second.key : value.path);
    for (const auto *layers : {&value.overrides, &value.surface_overrides, &value.displacement_overrides}) {
      add(std::to_string(layers->size()));
      for (const auto &layer : *layers)
        add(layer);
    }
  };
  assignment(request.material_assignment());
  add(request.stylesheet);
  add(request.object_material_revision);
  if (request.native_styles)
    add(request.native_styles->signature);
  for (const auto &sample : request.object_material_samples) {
    add(std::to_string(std::bit_cast<std::uint64_t>(sample.time)));
    assignment(sample.assignment);
    add(sample.stylesheet);
    for (const auto *fields : {&sample.errors, &sample.resources}) {
      add(std::to_string(fields->size()));
      for (const auto &value : *fields)
        add(value);
    }
  }
  const auto hash = XXH3_128bits(text.data(), text.size());
  values.push_back(std::bit_cast<std::int64_t>(hash.low64));
  values.push_back(std::bit_cast<std::int64_t>(hash.high64));
}
namespace {
class StyleProfile {
public:
  explicit StyleProfile(std::map<std::string, double> *times) : times_(times) {
    if (times_)
      last_ = Clock::now();
  }
  void mark(const char *name) {
    if (times_) {
      const auto now = Clock::now();
      (*times_)[name] += std::chrono::duration<double>(now - last_).count();
      last_ = now;
    }
  }

private:
  using Clock = std::chrono::steady_clock;
  std::map<std::string, double> *times_;
  Clock::time_point last_;
};
std::string quoted(const std::string &value) {
  std::ostringstream out;
  out << '"';
  for (const unsigned char c : value) {
    if (c == '"' || c == '\\')
      out << '\\' << c;
    else if (c < 32)
      out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c) << std::dec;
    else
      out << c;
  }
  out << '"';
  return out.str();
}
std::optional<std::string> literal(const UT_OptionEntry &value) {
  std::ostringstream out;
  out << std::setprecision(17);
  switch (value.optionStorage()) {
  case UT_OptionEntry::UT_OPTION_STORE_BOOL: {
    bool v;
    if (!value.importOption(v))
      return {};
    out << int(v);
    break;
  }
  case UT_OptionEntry::UT_OPTION_STORE_INT: {
    UT_Array<int64> v;
    if (!value.importOption(v) || v.size() > 4)
      return {};
    if (v.size() != 1)
      out << '[';
    for (exint i = 0; i < v.size(); ++i) {
      if (i)
        out << ',';
      out << v[i];
    }
    if (v.size() != 1)
      out << ']';
    break;
  }
  case UT_OptionEntry::UT_OPTION_STORE_REAL: {
    UT_Array<fpreal64> v;
    if (!value.importOption(v) || v.size() > 4)
      return {};
    if (v.size() != 1)
      out << '[';
    for (exint i = 0; i < v.size(); ++i) {
      if (!std::isfinite(v[i]))
        return {};
      if (i)
        out << ',';
      out << v[i];
    }
    if (v.size() != 1)
      out << ']';
    break;
  }
  case UT_OptionEntry::UT_OPTION_STORE_STRING: {
    UT_StringHolder v;
    if (value.optionSize() != 1 || !value.importOption(v))
      return {};
    return quoted(v.toStdString());
  }
  default:
    return {};
  }
  return out.str();
}
void issue(NativeMaterialStyles &result, const std::string &reason) {
  if (std::any_of(result.diagnostics.begin(), result.diagnostics.end(),
                  [&](const auto &d) { return d.message == reason; }))
    return;
  result.diagnostics.push_back({"material.stylesheet_ignored",
                                "material_stylesheet",
                                {},
                                reason,
                                "Keeping supported stylesheet results, underlying material bindings and geometry",
                                "warning",
                                "material_stylesheet"});
}
NativeMaterialStyles::Layer layer(const STY_Results &results, NativeMaterialStyles &result) {
  NativeMaterialStyles::Layer output;
  for (const auto &[category, values] : results) {
    if (category == stylesheet_recovery_category) {
      for (const auto &[name, value] : values) {
        UT_StringHolder reason;
        if (value.myValue && value.myValue->importOption(reason) && reason.isstring())
          issue(result, reason.toStdString());
      }
    } else if (category == STY_OVERRIDE_MAT) {
      for (const auto &[name, value] : values) {
        UT_StringHolder path;
        if (name == "name" && value.myValue && value.myValue->importOption(path))
          output.material = path.toStdString();
        else
          issue(result, "Unsupported stylesheet material property: " + name.toStdString());
      }
    } else if (category == STY_OVERRIDE_MAT_PARMS || category == STY_OVERRIDE_SURFACE_PARMS ||
               category == STY_OVERRIDE_DISPLACE_PARMS) {
      auto &parameters = category == STY_OVERRIDE_SURFACE_PARMS    ? output.surface_parameters
                         : category == STY_OVERRIDE_DISPLACE_PARMS ? output.displacement_parameters
                                                                   : output.parameters;
      std::map<std::string, std::string> fields;
      for (const auto &[name, value] : values) {
        const auto owned = value.myValue ? literal(*value.myValue) : std::nullopt;
        if (owned)
          fields.emplace(name.toStdString(), *owned);
        else
          issue(result, "Unsupported stylesheet parameter value: " + category.toStdString() + "." + name.toStdString());
      }
      if (fields.size() > 128)
        issue(result, "Stylesheet parameter set exceeds 128 fields; omitting this parameter layer");
      else if (!fields.empty()) {
        parameters = "{";
        for (const auto &[name, value] : fields) {
          if (parameters.size() > 1)
            parameters += ",";
          parameters += quoted(name) + ":" + value;
        }
        parameters += "}";
      }
      if (parameters.size() > 65536) {
        parameters.clear();
        issue(result, "Stylesheet parameter set exceeds 64 KiB; omitting this parameter layer");
      }
    } else if (!values.empty())
      issue(result, "Unsupported stylesheet override category: " + category.toStdString());
  }
  return output;
}
class FrozenStyleBatch {
public:
  FrozenStyleBatch(NativeMaterialStyles::Layers &layers, NativeMaterialStyles &result)
      : layers_(layers), result_(result) {
    key_.reserve(32);
  }
  std::uint32_t slot(const STY_Results &results) {
    key_.clear();
    // Native result values and string names stay alive in this batch's
    // UT_Array. Pointer identity only avoids repeated constant conversion;
    // owned layer interning still proves exact value equality. No pointer
    // from this lookup survives the subject/result group.
    for (const auto &[category, values] : results) {
      key_.push_back(reinterpret_cast<std::uintptr_t>(category.c_str()));
      key_.push_back(values.size());
      for (const auto &[name, value] : values) {
        key_.push_back(reinterpret_cast<std::uintptr_t>(name.c_str()));
        key_.push_back(reinterpret_cast<std::uintptr_t>(value.myValue.get()));
      }
    }
    if (const auto found = values_.find(key_); found != values_.end())
      return found->second;
    const auto index = layers_.intern(layer(results, result_));
    values_.emplace(key_, index);
    return index;
  }

private:
  NativeMaterialStyles::Layers &layers_;
  NativeMaterialStyles &result_;
  std::vector<std::uintptr_t> key_;
  std::map<std::vector<std::uintptr_t>, std::uint32_t> values_;
};
std::string signature(const NativeMaterialStyles &result) {
  std::string text;
  const auto add = [&](const std::string &s) { text += std::to_string(s.size()) + ":" + s; };
  for (const auto *list : {&result.primitives, &result.points, &result.point_context_layers}) {
    add(std::to_string(list->values().size()));
    for (const auto &l : list->values()) {
      add(l.material);
      add(l.parameters);
      add(l.surface_parameters);
      add(l.displacement_parameters);
    }
    add(std::to_string(list->slots().size()));
    if (!list->slots().empty())
      text.append(reinterpret_cast<const char *>(list->slots().data()), list->slots().size() * sizeof(std::uint32_t));
  }
  add(std::to_string(result.point_contexts.size()));
  for (const auto &[key, slot] : result.point_contexts) {
    add(std::to_string(key.first));
    add(std::to_string(key.second));
    add(std::to_string(slot));
  }
  for (const auto owner : result.point_context_primitives)
    add(std::to_string(owner));
  add(result.default_descendant);
  add(std::to_string(result.descendants.size()));
  for (const auto &[index, s] : result.descendants) {
    add(std::to_string(index));
    add(s);
  }
  const auto hash = XXH3_128bits(text.data(), text.size());
  return std::to_string(hash.high64) + ":" + std::to_string(hash.low64);
}
bool point_targets(const STY_TargetHandle &target) {
  return target && (target->getType().isPoint() || point_targets(target->getSubTarget()));
}
bool point_targets(const std::string &text) {
  if (text.empty() || text == "{}")
    return false;
  STY_StyleSheetHandle sheet(new STY_StyleSheet(text.c_str(), "Cycles point targets", STY_LOAD_FOR_STYLING));
  return sheet->getImportFiles().size() != 0 ||
         std::any_of(sheet->getStyles().begin(), sheet->getStyles().end(),
                     [](const auto &style) { return point_targets(style->getTarget()); });
}
class SourcePointSubjects final : public STY_SubjectGroup {
public:
  SourcePointSubjects(const GU_Detail &source, const std::vector<std::size_t> &indices, bool detail,
                      const std::function<bool(std::size_t)> &accepted)
      : indices(indices) {
    for (std::size_t first = 0; first < indices.size();) {
      auto end = first + 1;
      const bool local = accepted(indices[first]);
      while (end < indices.size() && indices[end] == indices[end - 1] + 1 && accepted(indices[end]) == local)
        ++end;
      auto group = std::make_unique<GSTY_SubjectPointGroup>(source, int(indices[first]), int(indices[end - 1] + 1), 0,
                                                            detail, local);
      for (std::size_t i = first; i < end; ++i)
        subjects_.push_back(&group->getSubject(int(i - first)));
      groups_.push_back(std::move(group));
      first = end;
    }
  }
  int getNumSubjects() const override { return int(subjects_.size()); }
  const STY_Subject &getSubject(int index) const override { return *subjects_.at(std::size_t(index)); }
  std::vector<std::size_t> indices;

private:
  std::vector<std::unique_ptr<GSTY_SubjectPointGroup>> groups_;
  std::vector<const STY_Subject *> subjects_;
};
} // namespace
std::shared_ptr<const NativeMaterialStyles>
NativeStyleCache::prepare(const GU_Detail &source, const std::string &stylesheet,
                          const std::vector<std::string> &resources, double time, const std::string &cwd,
                          std::map<std::string, double> *timings, NativePreparationBudget *budget,
                          const NativeGeometryPartitions *partitions, const std::array<bool, 5> *render_types) {
  StyleProfile profile(timings);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Stylesheet acquisition requires the host thread");
  if (budget && capture_ != budget->capture) {
    captures_.clear();
    used_.clear();
    capture_ = budget->capture;
  }
  CH_AutoEvaluateTime evaluation_time(*CH_Manager::getContext(), SYSgetSTID(), time);
  UT_AutoErrorManager source_errors;
  const bool active = !stylesheet.empty() && stylesheet != "{}";
  bool authored = false;
  for (const auto owner : {GA_ATTRIB_DETAIL, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_POINT})
    authored |= source.findAttribute(owner, "material_stylesheet") != nullptr;
  if (!active && !authored && resources.empty())
    return {};
  std::vector<std::int64_t> stamp{source.getMetaCacheCount(), source.getTopology().getDataId(),
                                  source.getPrimitiveList().getDataId(), source.getNumPrimitives(),
                                  source.getNumPoints()};
  bool valid = stamp[1] != GA_INVALID_DATAID && stamp[2] != GA_INVALID_DATAID;
  for (const auto owner : {GA_ATTRIB_DETAIL, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_POINT, GA_ATTRIB_VERTEX})
    for (auto it = source.getAttributes().begin(owner); !it.atEnd(); ++it) {
      const auto id = it.attrib()->getDataId();
      stamp.push_back(id);
      valid &= id != GA_INVALID_DATAID;
    }
  auto context = std::to_string(cwd.size()) + ":" + cwd + stylesheet;
  for (const auto &resource : resources)
    context += std::to_string(resource.size()) + ":" + resource;
  for (const auto kind : {NativeGeometryKind::Points, NativeGeometryKind::Curves})
    context += render_types && !render_types->at(std::size_t(kind)) ? "/off" : "/on";
  const auto key = std::make_pair(source.getUniqueId(), context);
  used_.insert(key);
  auto &record = records_[key];
  if (valid && record.stamp == stamp && record.styles && !record.styles->scripted &&
      record.source_signature == stylesheet_source_signature(record.styles->resources))
    return record.styles;
  // Only owned values and owner indices survive a host turn. Script results
  // additionally belong to this capture AND time, including shutter visits.
  const auto capture_key = std::make_tuple(source.getUniqueId(), context, std::bit_cast<std::uint64_t>(time));
  Work synchronous;
  auto &work = budget ? captures_[capture_key] : synchronous;
  const auto unchanged = [&]() {
    std::vector<std::string> names, expected;
    for (const auto &[name, value] : work.watched) {
      names.push_back(name);
      expected.push_back(value);
    }
    return names.empty() || expected == stylesheet_source_signature(names);
  };
  if (work.started && (work.stamp != stamp || !unchanged()))
    work = Work{};
  const auto watch = [&](const std::vector<std::string> &names) {
    std::vector<std::string> added;
    for (const auto &name : names)
      if (!work.watched.contains(name))
        added.push_back(name);
    if (added.empty())
      return;
    const auto states = stylesheet_source_signature(added);
    if (states.size() != added.size())
      throw std::logic_error("Stylesheet dependency signature count mismatch");
    for (std::size_t i = 0; i < added.size(); ++i)
      work.watched.emplace(added[i], states[i]);
  };
  auto result = work.result;
  const auto safe = [&](const std::string &text) {
    if (text.empty() || text == "{}")
      return true;
    if (const auto found = work.checked.find(text); found != work.checked.end())
      return found->second;
    auto info = stylesheet_sources(text);
    watch(info.resources);
    result->scripted |= info.scripted;
    for (const auto &error : info.errors)
      issue(*result, error);
    work.checked.emplace(text, info.safe);
    return info.safe;
  };
  if (!work.started) {
    work.started = true;
    work.stamp = stamp;
    watch(resources);
    work.parent_safe = safe(stylesheet);
    work.point_targets = work.parent_safe && point_targets(stylesheet);
  }
  if (work.done)
    return result;
  // Attribute validation is incremental too. A continuation stores ordered
  // owner indices, never GA iterators/handles into a borrowed detail.
  const std::array owners{GA_ATTRIB_DETAIL, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_POINT};
  const auto point_scope = [&]() {
    if (work.point_scope_ready)
      return;
    profile.mark("style_sources");
    // The capture normally supplies its cached partitions. A synchronous
    // caller may omit them; retain only their owned offset metadata if this
    // preparation subsequently yields, never a borrowed GA iterator.
    if (!partitions && !work.scope_partitions)
      work.scope_partitions = std::make_shared<const NativeGeometryPartitions>(partition_geometry(source));
    const auto &selections = partitions ? *partitions : *work.scope_partitions;
    std::array<const NativeGeometrySelection *, 2> scopes{};
    std::array<bool, 2> standalone{};
    constexpr std::array kinds{NativeGeometryKind::Points, NativeGeometryKind::Curves};
    for (std::size_t i = 0; i < kinds.size(); ++i) {
      const auto kind = kinds[i];
      if (render_types && !render_types->at(std::size_t(kind)))
        continue;
      const auto *selection = selections.find(kind);
      if (!selection)
        continue;
      standalone[i] = kind == NativeGeometryKind::Points &&
                      (selection->full_detail ? source.getNumPrimitives() == 0 : selection->primitives.entries() == 0);
      if (standalone[i] || source.findPointAttribute("material_stylesheet") || work.point_targets)
        scopes[i] = selection;
    }
    const auto boundary = [&]() {
      if (budget && budget->expired()) {
        profile.mark("style_point_scope");
        throw NativePreparationPending{};
      }
    };
    std::size_t acquired = 0;
    while (work.scope_primitive_kind < scopes.size()) {
      const auto i = work.scope_primitive_kind;
      const auto *selection = scopes[i];
      const auto count =
          selection && !standalone[i]
              ? std::size_t(selection->full_detail ? source.getNumPrimitives() : selection->primitives.entries())
              : 0;
      while (work.scope_primitive[i] < count) {
        const auto index = work.scope_primitive[i]++;
        const auto offset =
            selection->full_detail ? source.primitiveOffset(GA_Index(index)) : selection->primitives(exint(index));
        work.point_primitives.push_back(std::size_t(source.primitiveIndex(offset)));
        if (++acquired % 256 == 0)
          boundary();
      }
      ++work.scope_primitive_kind;
    }
    // Partition point lists follow the source point map. Merge their two
    // ordered streams incrementally so shared particle/curve keys appear
    // exactly once, without an unbounded set insertion or final sort/copy.
    for (;;) {
      std::array<std::size_t, 2> next;
      next.fill(std::numeric_limits<std::size_t>::max());
      for (std::size_t i = 0; i < scopes.size(); ++i)
        if (const auto *selection = scopes[i]) {
          const auto count = std::size_t(selection->full_detail ? source.getNumPoints() : selection->points.entries());
          if (work.scope_point[i] < count) {
            const auto index = work.scope_point[i];
            const auto offset =
                selection->full_detail ? source.pointOffset(GA_Index(index)) : selection->points(exint(index));
            next[i] = std::size_t(source.pointIndex(offset));
          }
        }
      const auto index = std::min(next[0], next[1]);
      if (index == std::numeric_limits<std::size_t>::max())
        break;
      work.point_scope.push_back(index);
      for (std::size_t i = 0; i < next.size(); ++i)
        if (next[i] == index) {
          ++work.scope_point[i];
          if (standalone[i])
            work.standalone_points.push_back(index);
        }
      if (++acquired % 256 == 0)
        boundary();
    }
    work.point_scope_ready = true;
    work.scope_partitions.reset();
    profile.mark("style_point_scope");
  };
  while (!work.validated && work.validation_owner < owners.size()) {
    const auto owner = owners[work.validation_owner];
    GA_ROHandleS handle(source.findAttribute(owner, "material_stylesheet"));
    if (owner == GA_ATTRIB_POINT)
      point_scope();
    if (!handle.isValid()) {
      ++work.validation_owner;
      work.validation_index = 0;
      continue;
    }
    const auto &map = handle.getAttribute()->getIndexMap();
    const auto count = owner == GA_ATTRIB_POINT ? work.point_scope.size() : std::size_t(map.indexSize());
    const auto end = std::min<std::size_t>(count, work.validation_index + 4096);
    while (work.validation_index < end) {
      const auto index = owner == GA_ATTRIB_POINT ? work.point_scope[work.validation_index] : work.validation_index;
      ++work.validation_index;
      const auto text = handle.get(map.offsetFromIndex(GA_Index(index))).toStdString();
      const auto checked = work.checked.size();
      if (!text.empty() && text != "{}") {
        work.local = true;
        const auto accepted = safe(text);
        if (owner == GA_ATTRIB_DETAIL)
          work.detail_safe &= accepted;
        if (owner == GA_ATTRIB_POINT)
          work.point_local = true;
        else if (accepted)
          work.point_targets |= point_targets(text);
      }
      if (budget && (work.checked.size() != checked || work.validation_index % 256 == 0) && budget->expired()) {
        profile.mark("style_sources");
        throw NativePreparationPending{};
      }
    }
    if (work.validation_index == count) {
      ++work.validation_owner;
      work.validation_index = 0;
    }
    if (budget && budget->expired() && work.validation_owner < owners.size()) {
      profile.mark("style_sources");
      throw NativePreparationPending{};
    }
  }
  work.validated = true;
  profile.mark("style_sources");
  NativeStyleEvaluation evaluation(time, cwd);
  const auto finish = [&]() {
    watch(evaluation.resources());
    if (budget && !unchanged()) {
      work = Work{};
      throw NativePreparationPending{};
    }
    result->primitives.seal();
    result->points.seal();
    result->point_context_layers.seal();
    for (const auto &[name, value] : work.watched)
      result->resources.push_back(name);
    profile.mark("style_finalize");
    result->signature = signature(*result);
    profile.mark("style_signature");
    record = {stamp, result, stylesheet_source_signature(result->resources)};
    work.done = true;
    return result;
  };
  STY_StyleSheetHandle sheet(new STY_StyleSheet(!work.parent_safe || stylesheet.empty() ? "{}" : stylesheet.c_str(),
                                                UT_StringHolder("Cycles stylesheet"), STY_LOAD_FOR_STYLING));
  auto checked_sheet = result->scripted ? evaluation.prepare(sheet) : sheet;
  result->scripted |= evaluation.scripted();
  if (sheet->getErrors().length())
    issue(*result, sheet->getErrors().buffer());
  STY_Styler parent(checked_sheet);
  STY_MaterialResultsFilter filter;
  if (!work.initialized && !work.local && !result->scripted && !parent.hasAnyPartialMatchStyles()) {
    STY_Results values;
    parent.getResults(values, filter);
    auto value = layer(values, *result);
    if (source.getNumPrimitives()) {
      result->primitives.push_back(value);
      if (!work.standalone_points.empty())
        result->points.push_back(value);
    } else
      result->points.push_back(std::move(value));
    UT_StringHolder json;
    parent.saveStyleSheet(json);
    result->default_descendant = parent.hasStyles() ? json.toStdString() : std::string{};
    return finish();
  }
  const bool primitive_subjects = source.getNumPrimitives() != 0;
  const bool universal =
      !work.initialized && !work.local && !result->scripted && work.watched.empty() &&
      sheet->getImportFiles().size() == 0 && (!primitive_subjects || work.standalone_points.empty()) &&
      (primitive_subjects || source.getNumPoints() != 0) &&
      std::all_of(sheet->getStyles().begin(), sheet->getStyles().end(), [&](const auto &style) {
        const auto &target = style->getTarget();
        return target && !target->getSubTarget() &&
               target->getTargetRequirements().begin() == target->getTargetRequirements().end() &&
               (!target->getPreBindings() || target->getPreBindings()->size() == 0) &&
               style->getOverrideBindingDataNames().size() == 0 &&
               (target->getType().isSelf() ||
                (primitive_subjects ? target->getType().isPrimitive() : target->getType().isPoint()));
      });
  if (universal) {
    const auto evaluate = [&](const STY_Subject &subject) {
      UT_WorkBuffer errors;
      STY_Results values;
      const auto matched = parent.cloneWithSubject(subject, &errors);
      matched.getResults(values, filter);
      (primitive_subjects ? result->primitives : result->points).push_back(layer(values, *result));
      UT_StringHolder json;
      matched.saveStyleSheet(json);
      result->default_descendant = matched.hasStyles() ? json.toStdString() : std::string{};
      if (errors.length())
        issue(*result, errors.buffer());
    };
    if (primitive_subjects) {
      GSTY_SubjectPrimGroup subjects(source, 0, 1, 0, 0, work.detail_safe, false);
      evaluate(subjects.getSubject(0));
    } else {
      GSTY_SubjectPointGroup subjects(source, 0, 1, 0, work.detail_safe, false);
      evaluate(subjects.getSubject(0));
    }
    return finish();
  }
  if (!work.initialized) {
    if (source.getNumPrimitives() > std::numeric_limits<int>::max() ||
        source.getNumPoints() > std::numeric_limits<int>::max())
      throw std::overflow_error("Native stylesheet owner index is outside HDK limits");
    if (primitive_subjects)
      result->primitives.resize(std::size_t(source.getNumPrimitives()));
    else
      result->points.resize(std::size_t(source.getNumPoints()));
    if (primitive_subjects && !work.standalone_points.empty())
      result->points.resize(std::size_t(source.getNumPoints()));
    if (work.local || result->scripted)
      work.subject_limit = work.local ? 16 : 64;
    work.initialized = true;
  }
  profile.mark("style_setup");
  const auto boundary = [&](auto began, int count, bool more) {
    watch(evaluation.resources());
    if (budget) {
      const auto duration = std::chrono::duration<double>(std::chrono::steady_clock::now() - began).count();
      const auto factor = std::clamp(.004 / std::max(duration, .000001), .5, 2.);
      work.subject_limit =
          std::clamp(int(count * factor), work.local || result->scripted ? 1 : 256, result->scripted ? 1024 : 32768);
      if (more && budget->expired())
        throw NativePreparationPending{};
    }
  };
  if (primitive_subjects) {
    GA_ROHandleS attribute(source.findPrimitiveAttribute("material_stylesheet"));
    const auto accepted = [&](int i) {
      return !attribute.isValid() || safe(attribute.get(source.primitiveOffset(i)).toStdString());
    };
    while (work.next_primitive < std::size_t(source.getNumPrimitives())) {
      const auto began = std::chrono::steady_clock::now();
      const auto first = int(work.next_primitive);
      int last = first + 1;
      const bool use_prim = accepted(first);
      const auto limit =
          std::min<std::int64_t>(source.getNumPrimitives(),
                                 std::int64_t(first) + std::min(work.subject_limit, result->scripted ? 1024 : 32768));
      while (last < limit && accepted(last) == use_prim)
        ++last;
      {
        GSTY_SubjectPrimGroup subjects(source, first, last, 0, 0, work.detail_safe, use_prim);
        STY_StylerGroup group;
        UT_Array<STY_Results> values;
        std::vector<STY_Styler> styles;
        if (result->scripted)
          styles = evaluation.evaluate({sheet}, subjects, values);
        else {
          group.append(parent, subjects);
          group.getResults(values, filter);
        }
        profile.mark("style_native");
        if (values.size() != last - first)
          throw std::logic_error("Native stylesheet results do not match primitive subjects");
        FrozenStyleBatch frozen(result->primitives, *result);
        for (exint i = 0; i < values.size(); ++i) {
          result->primitives.set(std::size_t(first + i), frozen.slot(values[i]));
          if (dynamic_cast<const GU_PrimPacked *>(&subjects.getPrimitive(int(i)))) {
            const auto styler = styles.empty() ? group.getStyler(int(i)) : styles[std::size_t(i)];
            if (result->scripted)
              result->descendants[std::size_t(first + i)] = evaluation.save(styler);
            else {
              UT_StringHolder json;
              styler.saveStyleSheet(json);
              result->descendants[std::size_t(first + i)] = json.toStdString();
            }
          }
        }
        profile.mark("style_freeze");
        UT_WorkBuffer errors;
        for (int i = 0; i < group.getNumStylers(); ++i)
          group.getStyler(i).getErrors(errors);
        if (errors.length())
          issue(*result, errors.buffer());
        profile.mark("style_errors");
      }
      profile.mark("style_retire");
      work.next_primitive = std::size_t(last);
      boundary(began, last - first, work.next_primitive < std::size_t(source.getNumPrimitives()));
    }
  } else {
    GA_ROHandleS attribute(source.findPointAttribute("material_stylesheet"));
    const auto accepted = [&](int i) {
      return !attribute.isValid() || safe(attribute.get(source.pointOffset(i)).toStdString());
    };
    while (work.next_point < std::size_t(source.getNumPoints())) {
      const auto began = std::chrono::steady_clock::now();
      const auto first = int(work.next_point);
      int last = first + 1;
      const bool use_point = accepted(first);
      const auto limit = std::min<std::int64_t>(
          source.getNumPoints(), std::int64_t(first) + std::min(work.subject_limit, result->scripted ? 1024 : 32768));
      while (last < limit && accepted(last) == use_point)
        ++last;
      {
        GSTY_SubjectPointGroup subjects(source, first, last, 0, work.detail_safe, use_point);
        STY_StylerGroup group;
        UT_Array<STY_Results> values;
        if (result->scripted)
          evaluation.evaluate({sheet}, subjects, values);
        else {
          group.append(parent, subjects);
          group.getResults(values, filter);
        }
        profile.mark("style_native");
        if (values.size() != last - first)
          throw std::logic_error("Native stylesheet results do not match point subjects");
        FrozenStyleBatch frozen(result->points, *result);
        for (exint i = 0; i < values.size(); ++i)
          result->points.set(std::size_t(first + i), frozen.slot(values[i]));
        profile.mark("style_freeze");
        UT_WorkBuffer errors;
        for (int i = 0; i < group.getNumStylers(); ++i)
          group.getStyler(i).getErrors(errors);
        if (errors.length())
          issue(*result, errors.buffer());
        profile.mark("style_errors");
      }
      profile.mark("style_retire");
      work.next_point = std::size_t(last);
      boundary(began, last - first, work.next_point < std::size_t(source.getNumPoints()));
    }
  }
  if (primitive_subjects && (work.point_local || work.point_targets)) {
    GA_ROHandleS primitive_style(source.findPrimitiveAttribute("material_stylesheet"));
    GA_ROHandleS point_style(source.findPointAttribute("material_stylesheet"));
    const auto accepted = [&](std::size_t index) {
      return !point_style.isValid() || safe(point_style.get(source.pointOffset(GA_Index(index))).toStdString());
    };
    while (work.next_context < work.point_primitives.size()) {
      const auto began = std::chrono::steady_clock::now();
      const auto index = work.point_primitives[work.next_context];
      const auto offset = source.primitiveOffset(GA_Index(index));
      const auto *primitive = source.getPrimitive(offset);
      result->point_context_primitives.insert(index);
      std::vector<std::size_t> keys;
      const auto end = std::min<std::size_t>(std::size_t(primitive->getVertexCount()), work.context_vertex + 256);
      while (work.context_vertex < end) {
        const auto vertex = primitive->getVertexOffset(GA_Size(work.context_vertex++));
        const auto point = std::size_t(source.pointIndex(source.vertexPoint(vertex)));
        if (!result->point_contexts.contains({index, point}))
          keys.push_back(point);
      }
      std::sort(keys.begin(), keys.end());
      keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
      if (!keys.empty()) {
        const bool use_primitive = !primitive_style.isValid() || safe(primitive_style.get(offset).toStdString());
        GSTY_SubjectPrimGroup ancestor(source, int(index), int(index + 1), 0, 0, work.detail_safe, use_primitive);
        SourcePointSubjects subjects(source, keys, false, accepted);
        UT_Array<STY_Results> values;
        evaluation.evaluate({sheet}, subjects, values, &ancestor.getSubject(0));
        if (values.size() != exint(keys.size()))
          throw std::logic_error("Native point-context results do not match source keys");
        FrozenStyleBatch frozen(result->point_context_layers, *result);
        for (std::size_t i = 0; i < keys.size(); ++i)
          result->point_contexts.emplace(std::make_pair(index, keys[i]), frozen.slot(values[exint(i)]));
      }
      if (work.context_vertex == std::size_t(primitive->getVertexCount())) {
        ++work.next_context;
        work.context_vertex = 0;
      }
      profile.mark("style_point_contexts");
      boundary(began, int(std::max<std::size_t>(1, keys.size())), work.next_context < work.point_primitives.size());
    }
  }
  if (primitive_subjects && !work.standalone_points.empty()) {
    GA_ROHandleS attribute(source.findPointAttribute("material_stylesheet"));
    const auto accepted = [&](std::size_t index) {
      return !attribute.isValid() || safe(attribute.get(source.pointOffset(GA_Index(index))).toStdString());
    };
    while (work.next_standalone < work.standalone_points.size()) {
      const auto began = std::chrono::steady_clock::now();
      const auto end = std::min(work.standalone_points.size(), work.next_standalone + 256);
      const std::vector<std::size_t> keys(work.standalone_points.begin() + std::ptrdiff_t(work.next_standalone),
                                          work.standalone_points.begin() + std::ptrdiff_t(end));
      SourcePointSubjects subjects(source, keys, work.detail_safe, accepted);
      UT_Array<STY_Results> values;
      evaluation.evaluate({sheet}, subjects, values);
      FrozenStyleBatch frozen(result->points, *result);
      if (values.size() != exint(keys.size()))
        throw std::logic_error("Native loose-point stylesheet results do not match source points");
      for (std::size_t i = 0; i < keys.size(); ++i)
        result->points.set(keys[i], frozen.slot(values[exint(i)]));
      work.next_standalone = end;
      profile.mark("style_loose_points");
      boundary(began, int(keys.size()), end < work.standalone_points.size());
    }
  }
  return finish();
}
void NativeStyleCache::retire(const std::set<std::int64_t> &details) {
  std::erase_if(records_, [&](const auto &v) { return !details.contains(v.first.first) || !used_.contains(v.first); });
  used_.clear();
  captures_.clear();
  capture_ = 0;
}
NativeMaterialAssignment styled_material(NativeMaterialAssignment binding, const NativeMaterialStyles::Layer &style) {
  const auto path = style.material.starts_with("op:") ? style.material.substr(3) : style.material;
  if (!path.empty() && path != binding.path) {
    binding.path = path;
    binding.overrides.clear();
    binding.surface_overrides.clear();
    binding.displacement_overrides.clear();
  }
  if (!style.parameters.empty() && !binding.path.empty())
    binding.overrides.push_back(style.parameters);
  if (!style.surface_parameters.empty() && !binding.path.empty())
    binding.surface_overrides.push_back(style.surface_parameters);
  if (!style.displacement_parameters.empty() && !binding.path.empty())
    binding.displacement_overrides.push_back(style.displacement_parameters);
  return binding;
}
NativeExternalMaterialStyle external_uniform_style(const std::string &text, double time, const std::string &cwd) {
  NativeExternalMaterialStyle output;
  if (text.empty() || text == "{}")
    return output;
  const auto sources = stylesheet_sources(text);
  output.resources = sources.resources;
  NativeMaterialStyles results;
  for (const auto &error : sources.errors)
    issue(results, error);
  if (!sources.safe) {
    output.diagnostics = std::move(results.diagnostics);
    return output;
  }
  struct UniformPrimitive final : STY_Subject {
    mutable bool selective{};
    STY_TargetMatchStatus matchesStyleTarget(const STY_TargetHandle &target) const override {
      if (!target)
        return STY_TargetMatchStatus(true);
      const bool uniform = !target->getSubTarget() &&
                           target->getTargetRequirements().begin() == target->getTargetRequirements().end() &&
                           (!target->getPreBindings() || target->getPreBindings()->size() == 0) &&
                           (target->getType().isSelf() || target->getType().isPrimitive());
      if (!uniform)
        selective = true;
      return STY_TargetMatchStatus(uniform);
    }
  } subject;
  STY_StyleSheetHandle sheet(new STY_StyleSheet(text.c_str(), UT_StringHolder(cwd), STY_LOAD_FOR_STYLING));
  if (sheet->getErrors().length())
    issue(results, sheet->getErrors().buffer());
  NativeStyleEvaluation evaluation(time, cwd);
  const auto styled = evaluation.object({sheet}, subject);
  STY_Results values;
  STY_MaterialResultsFilter filter;
  styled.getResults(values, filter);
  output.layer = layer(values, results);
  output.diagnostics = std::move(results.diagnostics);
  if (subject.selective)
    output.diagnostics.push_back(
        {"material.external_stylesheet",
         cwd,
         {},
         "Selective Houdini stylesheet targets require Blender polygon, group or point subjects that are unavailable "
         "on direct external meshes",
         "Keeping blanket stylesheet assignments and parameter overrides; omitting selective rules",
         "warning",
         "material_stylesheet"});
  const auto resources = evaluation.resources();
  output.resources.insert(output.resources.end(), resources.begin(), resources.end());
  return output;
}
std::string descendant_styles(const GeometryRequest &request, const GU_Detail &source, GA_Offset offset) {
  if (!request.native_styles)
    return {};
  const auto &styles = *request.native_styles;
  const auto index = std::size_t(source.primitiveIndex(offset));
  const auto found = styles.descendants.find(index);
  return found == styles.descendants.end() ? styles.default_descendant : found->second;
}
const NativeStyleBindingCache::Assignments &
NativeStyleBindingCache::assignments(const GU_Detail &source, const NativeGeometrySelection &selection,
                                     const GeometryRequest &request, const NativeMaterialBindings &bindings,
                                     NativePreparationBudget &budget, bool can_yield) {
  if (capture_ != budget.capture) {
    clear();
    capture_ = budget.capture;
  }
  const Key key{source.getUniqueId(), int(selection.kind), request.material_assignment(),
                request.native_styles ? request.native_styles->signature : std::string{}, bindings.ignored_overrides()};
  auto &work = work_[key];
  const auto primitive_count =
      std::size_t(selection.full_detail ? source.getNumPrimitives() : selection.primitives.entries());
  const bool particles = selection.kind == NativeGeometryKind::Points && primitive_count != 0;
  const bool curves = selection.kind == NativeGeometryKind::Curves;
  const auto point_count = selection.kind == NativeGeometryKind::Points && !particles
                               ? std::size_t(selection.full_detail ? source.getNumPoints() : selection.points.entries())
                               : 0;
  const auto add = [&](NativeMaterialAssignment value) {
    const auto [found, inserted] = work.assignments.values.insert(std::move(value));
    if (curves && inserted && found->has_overrides()) {
      ++work.assignments.override_variants;
      for (const auto *layers : {&found->overrides, &found->surface_overrides, &found->displacement_overrides})
        for (const auto &layer : *layers)
          work.assignments.override_bytes += layer.size();
      work.assignments.override_excess =
          work.assignments.override_variants > 4096 || work.assignments.override_bytes > 8 * 1024 * 1024;
    }
  };
  while (!work.done) {
    std::size_t visited = 0;
    while (work.primitive < primitive_count && visited < 256) {
      const auto index = work.primitive;
      const auto offset =
          selection.full_detail ? source.primitiveOffset(GA_Index(index)) : selection.primitives(exint(index));
      if (particles || (curves && bindings.curve_key_layers())) {
        const auto *primitive = source.getPrimitive(offset);
        if (particles && !dynamic_cast<const GEO_PrimParticle *>(primitive))
          throw std::logic_error("Non-particle in prepared point stylesheet selection");
        if (curves && !work.curve)
          work.curve = bindings.curve_binding(offset);
        while (work.vertex < std::size_t(primitive->getVertexCount()) && visited < 256) {
          const auto vertex = primitive->getVertexOffset(GA_Size(work.vertex++));
          ++visited;
          if (particles)
            work.assignments.values.insert(bindings.point_assignment(source.vertexPoint(vertex), offset));
          else
            bindings.curve_key(*work.curve, vertex);
        }
        if (work.vertex < std::size_t(primitive->getVertexCount()))
          break;
        if (curves) {
          unsigned omitted = 0;
          work.assignments.active_overrides |= work.curve->active_overrides();
          add(work.curve->assignment(&omitted));
          const auto example = std::size_t(source.primitiveIndex(offset));
          if (omitted & 1u) {
            ++work.assignments.point_curves;
            if (work.assignments.point_examples.size() < 16)
              work.assignments.point_examples.push_back(example);
          }
          if (omitted & 2u) {
            ++work.assignments.vertex_curves;
            if (work.assignments.vertex_examples.size() < 16)
              work.assignments.vertex_examples.push_back(example);
          }
          if (omitted & 4u) {
            ++work.assignments.styled_curves;
            if (work.assignments.style_examples.size() < 16)
              work.assignments.style_examples.push_back(example);
          }
          work.curve.reset();
        }
        work.vertex = 0;
        ++work.primitive;
        // Empty curves/particle primitives still advance this batch.
        ++visited;
      } else {
        add(bindings.assignment(offset));
        ++work.primitive;
        ++visited;
      }
      if (curves && work.assignments.override_excess && work.assignments.active_overrides) {
        work.done = true;
        return work.assignments;
      }
    }
    if (work.primitive < primitive_count) {
      if (can_yield && budget.expired())
        throw NativePreparationPending{};
      continue;
    }
    const auto point_end = std::min(point_count, work.point + 256);
    while (work.point < point_end) {
      const auto index = work.point++;
      const auto offset = selection.full_detail ? source.pointOffset(GA_Index(index)) : selection.points(exint(index));
      work.assignments.values.insert(bindings.point_assignment(offset));
    }
    work.done = work.point == point_count;
    if (!work.done && can_yield && budget.expired())
      throw NativePreparationPending{};
  }
  return work.assignments;
}
std::shared_ptr<const NativeMaterialLookup>
freeze_styled_materials(const GU_Detail &source, const NativeGeometrySelection &selection,
                        const GeometryRequest &request, std::shared_ptr<const NativeMaterialLookup> base, bool volume,
                        NativeStyleBindingCache *cache, NativePreparationBudget *budget) {
  const bool curves = selection.kind == NativeGeometryKind::Curves;
  const bool curve_keys = curves && NativeMaterialBindings(source, request, &selection).curve_key_layers();
  if (!request.native_styles && !curve_keys)
    return base;
  auto result = std::make_shared<NativeMaterialLookup>();
  result->policies = base->policies;
  result->displacement_only = base->displacement_only;
  result->diagnostics = base->diagnostics;
  if (curve_keys) {
    auto policy = std::make_shared<NativeMaterialPolicy>();
    const auto existing = base->policies.find(int(selection.kind));
    const auto fallback = base->policies.find(-1);
    if (existing != base->policies.end() && existing->second)
      *policy = *existing->second;
    else if (fallback != base->policies.end() && fallback->second)
      *policy = *fallback->second;
    NativeStyleBindingCache synchronous;
    NativePreparationBudget complete;
    auto &capture = cache && budget ? *cache : synchronous;
    auto &allowance = cache && budget ? *budget : complete;
    for (;;) {
      auto owned_request = request;
      owned_request.native_materials.reset();
      owned_request.native_material_policy = policy;
      NativeMaterialBindings bindings(source, owned_request, &selection);
      const auto &values = capture.assignments(source, selection, owned_request, bindings, allowance, cache && budget);
      if (values.override_excess && values.active_overrides) {
        unsigned dropped = 0;
        for (const auto mask : {8u, 4u, 2u, 1u})
          if (!(policy->ignored_overrides & mask) && (values.active_overrides & mask)) {
            dropped = mask;
            break;
          }
        if (!dropped)
          throw std::logic_error("Curve override capacity recovery has no contributing source layer");
        policy->ignored_overrides |= dropped;
        const std::string owner = dropped == 8   ? "vertex"
                                  : dropped == 4 ? "point"
                                  : dropped == 2 ? "primitive"
                                                 : "detail";
        result->diagnostics.push_back(
            {"material.override_capacity", request.sop_path, request.geometry_key,
             values.override_bytes > 8 * 1024 * 1024 ? "Whole-curve overrides exceed the 8 MiB owned-literal budget"
                                                     : "Whole-curve overrides exceed the 4096-variant budget",
             "Ignoring the " + owner + " override layer; retaining supported lower layers, material paths and curves",
             "warning", "material_override"});
        continue;
      }
      for (const auto &value : values.values)
        if (!value.path.empty())
          result->emplace(value, resolve_native_material(value, volume));
      result->unassigned =
          std::any_of(values.values.begin(), values.values.end(), [](const auto &value) { return value.path.empty(); });
      const auto diagnose = [&](std::size_t count, const auto &examples, const char *owner) {
        if (!count)
          return;
        std::string indices;
        for (const auto example : examples)
          indices += (indices.empty() ? "" : ", ") + std::to_string(example);
        result->diagnostics.push_back({"material.unsupported_binding", request.sop_path, request.geometry_key,
                                       "Varying " + std::string(owner) + " material bindings on " +
                                           std::to_string(count) + " curves (primitive examples: " + indices +
                                           ") cannot be represented by one shader per complete curve",
                                       "Ignoring the varying " + std::string(owner) +
                                           " layer on affected curves; retaining uniform lower layers and geometry"});
      };
      diagnose(values.point_curves, values.point_examples, "point");
      diagnose(values.vertex_curves, values.vertex_examples, "vertex");
      diagnose(values.styled_curves, values.style_examples, "point stylesheet");
      result->policies[int(selection.kind)] = std::move(policy);
      return result;
    }
  }
  NativeMaterialBindings bindings(source, request, &selection);
  const auto add = [&](const NativeMaterialAssignment &value) {
    if (!value.path.empty() && !result->contains(value))
      result->emplace(value, resolve_native_material(value, volume));
  };
  const auto &styles = *request.native_styles;
  // With no element-level path/override fields, the native fallback is
  // genuinely uniform. Every pooled layer is used in a full-detail selection;
  // resolve that exact set without reconstructing the same assignment for
  // every face. Partial selections and authored per-element bindings retain
  // their actual owner walk, including empty and mixed assignments.
  const bool point_contexts = selection.kind == NativeGeometryKind::Points && !styles.point_contexts.empty();
  if (selection.full_detail && !base->elements && !point_contexts) {
    const bool points = selection.kind == NativeGeometryKind::Points;
    const auto underlying = points ? bindings.uniform_point_assignment() : bindings.uniform_primitive_assignment();
    if (underlying && (!points || styles.primitives.empty())) {
      const auto &layers = points ? styles.points : styles.primitives;
      if (!layers.empty()) {
        for (const auto &value : layers.values())
          add(styled_material(*underlying, value));
        return result;
      }
    }
  }
  const auto *uniform = styles.primitives.size() == 1 ? &styles.primitives.front()
                        : selection.kind == NativeGeometryKind::Points && styles.points.size() == 1
                            ? &styles.points.front()
                            : nullptr;
  if (uniform && !base->elements && !point_contexts) {
    // Object/self rules share one layer. Transform/camera edits must not
    // walk a million primitive bindings just to freeze that same value.
    if (base->empty())
      add(styled_material({}, *uniform));
    else
      for (const auto &[value, reference] : *base)
        add(styled_material(value, *uniform));
    return result;
  }
  if (cache && budget) {
    for (const auto &value : cache->assignments(source, selection, request, bindings, *budget).values)
      add(value);
  } else {
    const auto primitives = selection.primitive_range(source);
    if (selection.kind == NativeGeometryKind::Points && !primitives.isEmpty()) {
      for (GA_Iterator it(primitives); !it.atEnd(); ++it) {
        const auto *primitive = dynamic_cast<const GEO_PrimParticle *>(source.getPrimitive(*it));
        if (!primitive)
          throw std::logic_error("Non-particle in prepared point stylesheet selection");
        for (const auto vertex : primitive->getVertexList())
          add(bindings.point_assignment(source.vertexPoint(vertex), *it));
      }
    } else {
      for (GA_Iterator it(primitives); !it.atEnd(); ++it)
        add(bindings.assignment(*it));
      if (selection.kind == NativeGeometryKind::Points)
        for (GA_Iterator it(selection.point_range(source)); !it.atEnd(); ++it)
          add(bindings.point_assignment(*it));
    }
  }
  return result;
}
} // namespace cycles_houdini
