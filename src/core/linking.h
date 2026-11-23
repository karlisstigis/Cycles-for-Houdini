// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <map>
#include <set>
#include <utility>
namespace cycles_houdini {
struct LinkingPlan {
  using Signature = std::pair<bool, std::vector<std::string>>;
  struct Domain {
    std::map<Signature, unsigned> sets;
    std::map<std::string, unsigned> objects;
    std::uint64_t membership(const std::string &emitter) const;
    unsigned set(const std::string &object) const;
    bool operator==(const Domain &) const = default;
  };
  Domain receivers, blockers;
  std::vector<SourceDiagnostic> diagnostics;
  bool operator==(const LinkingPlan &) const = default;
};
// Set zero remains unrestricted. Reuse accepted indices for surviving signatures
// and bound the retained plan to the pinned engine's 63 nondefault sets/domain.
LinkingPlan prepare_linking(const LinkingSettings &, const LinkingPlan &previous = {},
                            const std::set<std::string> *active = nullptr, bool world_active = false);
} // namespace cycles_houdini
