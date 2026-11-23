// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <cstdint>
#include <string>
#include <unordered_map>

namespace cycles_houdini {
inline const GA_Attribute *identity_attribute(const GA_Attribute *value, GA_StorageClass storage) {
  return value && value->getStorageClass() == storage && value->getTupleSize() == 1 ? value : nullptr;
}
// Identity attributes are optional hints, often left over after merges/sims.
// Count only this component's domain. Every member of a duplicate group falls
// back consistently; no first-member exception and no geometry is discarded.
class NativeIdentityReader {
public:
  NativeIdentityReader(const GA_Attribute *names, const GA_Attribute *ids, const GA_Range &range)
      : names_(identity_attribute(names, GA_STORECLASS_STRING)), ids_(identity_attribute(ids, GA_STORECLASS_INT)) {
    if (!names_.isValid() && !ids_.isValid())
      return;
    for (GA_Iterator it(range); !it.atEnd(); ++it) {
      if (names_.isValid()) {
        const auto name = names_.get(*it).toStdString();
        if (!name.empty())
          ++names_count_[name];
      }
      if (ids_.isValid())
        ++ids_count_[ids_.get(*it)];
    }
  }
  // The fallback index is local to the rendered component (or to a strand's
  // keys). Other primitive families may change counts and shift global GA
  // indices without changing this component's ordering or correspondence.
  std::string read(GA_Offset offset, std::int64_t index, std::uint64_t &fallback) const {
    if (names_.isValid()) {
      const auto name = names_.get(offset).toStdString();
      if (!name.empty() && names_count_.at(name) == 1)
        return "name:" + std::to_string(name.size()) + ":" + name;
    }
    if (ids_.isValid()) {
      const auto id = ids_.get(offset);
      if (ids_count_.at(id) == 1)
        return "id:" + std::to_string(id);
    }
    ++fallback;
    return "index:" + std::to_string(index);
  }

private:
  GA_ROHandleS names_;
  GA_ROHandleID ids_;
  std::unordered_map<std::string, std::size_t> names_count_;
  std::unordered_map<std::int64_t, std::size_t> ids_count_;
};
} // namespace cycles_houdini
