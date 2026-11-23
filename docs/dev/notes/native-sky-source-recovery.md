# Sky Texture source recovery

An unusable reachable Sky Texture now follows the same local source-recovery
policy as other unavailable shader inputs. Finite numeric values outside the
supported model's range and a zero-length legacy-model sun direction no longer
reject the whole scene. Valid model ranges and physical sky evaluation are
unchanged. Malformed owned graph types, sockets and enum tokens remain strict
internal errors.

The fallback depends on where the failed source is used:

- A world surface uses the ROP's configured background and reports
  `world.unavailable`, avoiding a magenta environment light.
- A surface color uses visible diagnostic magenta. Independent valid true
  displacement is retained. A shared dependency invalidates the dependent
  displacement branch; a height-only or normal-only fault disables that effect
  and retains usable surface shading and geometry.
- A sparse-volume material retains its valid density with magenta scattering
  and no diagnostic emission. It does not invent a density field.
- An analytic light retains its supported color, power, transform and shape
  while omitting the unusable shader modulation. The warning identifies the
  failed light shader.

Disconnected Sky nodes are ignored. Warnings survive an unchanged-source cache
hit and clear when the source is repaired. Independent object, camera and light
edits continue while a fault is present; repairing the source restores its
original image without restarting the persistent renderer.

## Verification

The evidence record (`artifacts/sky-source-recovery-evidence-20261020.json`)
selects fifteen passing source-audited runs on
`hosts/20261020T074805013/resources`. Tests-only restaging preserves all eight
recorded engine, binary, kernel and host-runtime identity fields. Earlier failed
test-authoring attempts are retained in the archive.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass thirty source-fault, warm
  capture, independent-edit and repair checks, plus five ordered final frames.
  Faulty frames contain diagnostics in their EXR metadata; every requested
  image is written successfully.
- Each device also passes eighteen physical/reference and domain checks. The
  four healthy sky models are compared against independently authored official
  Cycles XML images. The volume test checks both retained scattering under
  illumination and zero accidental emission against a black background.
- Seven analytic-light checks per device include an independent point-light
  photometric reference during fallback, light/camera edits, exact image repair
  and rejection of a malformed internal enum without changing the accepted
  generation.
- Each viewer separately passes twenty-four live states on each individual
  GPU. Tests edit actual Houdini expressions, wait for the current accepted
  image, verify the displayed pixels and release tracked resources on close.
- The CPU transaction contract passes fifty strict/local recovery checks. The
  existing scene-load test also passes on the new runtime with no callback
  dialogs, preserved saved-file hashes and a clean process exit.

Houdini clamps individual numeric Sky controls to their declared scalar ranges,
including expression evaluation. Live tests therefore author a zero sun vector
whose individual components are valid controls but whose combined direction is
unusable. Numeric range faults are tested separately through owned graph
payloads; the evidence does not claim that clamped parameter edits reached the
renderer.

These checks close the identified Sky source-recovery gap. They do not certify
general latency, prolonged process memory, all device combinations, every saved
ROP or artist desktop configuration, or release readiness. The broader native
production implementation remains active.
