// SPDX-License-Identifier: Apache-2.0
#include "linking.h"
#include <algorithm>
#include <array>
#include <set>
#include <stdexcept>
namespace cycles_houdini {
namespace {
void identity(const std::string &name) {
  if (name.empty() || name.size() > 4096 || name.find('\0') != std::string::npos)
    throw std::invalid_argument("Linking identities require 1..4096 bytes without NULs");
}
void prepare_domain(const std::vector<LinkingRule> &rules, const LinkingPlan::Domain &previous,
                    LinkingPlan::Domain &result, std::vector<SourceDiagnostic> &diagnostics, const char *domain,
                    const std::set<std::string> *active, bool world_active) {
  std::map<LinkingPlan::Signature, std::set<std::string>> grouped;
  std::set<std::string> assigned;
  for (const auto &rule : rules) {
    if (rule.objects.empty())
      throw std::invalid_argument("Linking rules require at least one object identity");
    for (const auto &name : rule.objects) {
      identity(name);
      if (!assigned.insert(name).second)
        throw std::invalid_argument("Conflicting linking rules for object: " + name);
    }
    auto emitters = rule.emitters;
    for (const auto &name : emitters)
      identity(name);
    const bool contains_world = std::find(emitters.begin(), emitters.end(), "world") != emitters.end();
    std::erase(emitters, std::string("world"));
    std::sort(emitters.begin(), emitters.end());
    emitters.erase(std::unique(emitters.begin(), emitters.end()), emitters.end());
    std::vector<std::string> live;
    for (const auto &object : rule.objects)
      if (!active || active->contains(object))
        live.push_back(object);
    if (live.empty())
      continue;
    if (world_active && contains_world == rule.exclude)
      diagnostics.push_back(
          {"linking.world_unsupported", live.front(), std::string("linking:world-") + domain,
           std::string("The pinned Cycles background path does not implement world ") + domain + " linking",
           "Retaining unrestricted world illumination and ordinary world occlusion; applying supported object/light "
           "links"});
    if (rule.exclude && emitters.empty())
      continue;
    auto &objects = grouped[{rule.exclude, std::move(emitters)}];
    objects.insert(live.begin(), live.end());
  }
  std::array<bool, 64> used{};
  used[0] = true;
  for (const auto &[signature, objects] : grouped)
    if (const auto found = previous.sets.find(signature); found != previous.sets.end()) {
      if (found->second == 0 || found->second >= 64 || used[found->second])
        throw std::logic_error("Invalid accepted linking allocation");
      used[found->second] = true;
      result.sets.emplace(signature, found->second);
    }
  for (const auto &[signature, objects] : grouped) {
    auto found = result.sets.find(signature);
    if (found == result.sets.end()) {
      auto free = std::find(used.begin() + 1, used.end(), false);
      if (free == used.end()) {
        std::string paths;
        unsigned shown = 0;
        for (const auto &path : objects) {
          if (shown++ == 8) {
            paths += " ...";
            break;
          }
          if (!paths.empty())
            paths += ", ";
          paths += path;
        }
        diagnostics.push_back(
            {"linking.limit", *objects.begin(), std::string("linking:") + domain,
             std::string("Cycles supports 63 nondefault ") + domain + " sets; omitted a group affecting " +
                 std::to_string(objects.size()) + " object roots: " + paths,
             "Rendering the affected objects with unrestricted linking; combine equivalent link selections"});
        continue;
      }
      const auto index = static_cast<unsigned>(free - used.begin());
      used[index] = true;
      found = result.sets.emplace(signature, index).first;
    }
    for (const auto &object : objects)
      result.objects.emplace(object, found->second);
  }
}
} // namespace
std::uint64_t LinkingPlan::Domain::membership(const std::string &emitter) const {
  if (emitter == "world")
    return ~std::uint64_t{};
  auto mask = ~std::uint64_t{};
  for (const auto &[signature, index] : sets) {
    const bool listed = std::binary_search(signature.second.begin(), signature.second.end(), emitter);
    if (listed == signature.first)
      mask &= ~(std::uint64_t(1) << index);
  }
  return mask;
}
unsigned LinkingPlan::Domain::set(const std::string &object) const {
  const auto found = objects.find(object);
  return found == objects.end() ? 0 : found->second;
}
LinkingPlan prepare_linking(const LinkingSettings &settings, const LinkingPlan &previous,
                            const std::set<std::string> *active, bool world_active) {
  LinkingPlan result;
  prepare_domain(settings.receivers, previous.receivers, result.receivers, result.diagnostics, "receiver", active,
                 world_active);
  prepare_domain(settings.blockers, previous.blockers, result.blockers, result.diagnostics, "shadow-blocker", active,
                 world_active);
  return result;
}
} // namespace cycles_houdini
