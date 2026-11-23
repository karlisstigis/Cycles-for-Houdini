// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>

namespace cycles_houdini {
// Read-only, opt-in telemetry for the whole calling process. GPU counters are
// OS-reported segment usage, not per-session allocations or exact residency.
// JSON preserves unavailable statuses. Windows queries node zero of each
// hardware DXGI adapter; linked multi-node devices require separate validation.
std::string process_memory_json();
} // namespace cycles_houdini
