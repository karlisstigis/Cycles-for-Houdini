// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>

namespace cycles_houdini {
// Owned source diagnostics travel with accepted resources. They are not engine
// errors and must not change the render/session lifecycle.
struct SourceDiagnostic {
  std::string code, source, resource, message, action;
  std::string severity{"warning"};
  std::string attribute; // Optional field identity, independent of the host node path.
  bool operator==(const SourceDiagnostic &) const = default;
};
// Recovery warnings indicate an authored input that was omitted or substituted.
// Informational notices and engine quality limits do not mean missing inputs.
inline bool missing_render_input(const SourceDiagnostic &issue) {
  return issue.severity != "info" && !issue.code.starts_with("render.") && issue.code != "volume.resampled";
}
} // namespace cycles_houdini
