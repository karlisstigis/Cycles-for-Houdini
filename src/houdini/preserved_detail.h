// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <GU/GU_DetailHandle.h>
#include <stdexcept>
#include <utility>

namespace cycles_houdini {
// A plain SOP handle preserves lifetime, but SOPs may still reuse its detail.
// Acquire this lease in the host cook context before handing const geometry to
// a converter. Keep it alive until every reader has finished. This does not
// authorize background OP evaluation, packed-source resolution or mutation.
class PreservedDetail {
public:
  explicit PreservedDetail(GU_ConstDetailHandle handle) : handle_(std::move(handle)) {
    if (!handle_)
      throw std::invalid_argument("Cannot preserve an empty SOP detail");
    handle_.addPreserveRequest();
  }
  ~PreservedDetail() { handle_.removePreserveRequest(); }
  PreservedDetail(const PreservedDetail &) = delete;
  PreservedDetail &operator=(const PreservedDetail &) = delete;
  const GU_Detail *get() const { return handle_.gdp(); }
  int preserves() const { return handle_.getPreserveRequest(); }

private:
  GU_ConstDetailHandle handle_;
};
} // namespace cycles_houdini
