# Contributing to Cycles for Houdini

Thank you for helping. This is an early project with one maintainer, so please keep changes small and explain them
well. You do not need to be a programmer to contribute: a clear problem report is a real contribution.

## Reporting a problem

Open an issue on the project's GitHub page. A report the maintainer can reproduce contains:

1. **What you did**, step by step, starting from an empty scene if you can.
2. **What you expected** and **what happened** instead. A screenshot or a small rendered image helps.
3. **Versions:** the project version (commit or release, `git log -1 --format=%h`), Houdini version and edition
   (22.0.466 is the tested build; say which 22.0 build you built for), Windows version, graphics card(s) and driver version, and the Blender version if a
   `.blend` file is involved.
4. **Device:** CPU or OptiX, and whether the problem also happens on the other one.
5. **The error text** and the relevant part of the log. For build problems, the last 50 lines of the build window or
   log; see "When a step fails" in [docs/user/build-it-yourself.md](docs/user/build-it-yourself.md).
6. **A small scene** that shows the problem, if you can share one. Remove anything you may not share.

Before posting, remove private information such as your user name in folder paths.

## Proposing a change

1. Make your own copy of the repository on GitHub (a fork) and create a branch for your change.
2. Read the [developer guide](docs/dev/README.md): the map of the repository, how to build and test, which tests
   to run for which change, the rules that must not be broken and known pitfalls.
3. Keep one topic per change. Add or extend a test that fails without your change.
4. Run the checks below and describe in the pull request what you changed, why, and which checks you ran. Say
   plainly which checks you could not run (for example because you have no NVIDIA GPU or no Blender).
5. Open a pull request against the main branch.

You are responsible for every line you submit, however it was written, and the same checks apply to all of it.

## Checks that must pass

| Check | Command |
| --- | --- |
| Formatting (clang-format 18.1.8, Ruff 0.14.3) | `python scripts/format-source.py` |
| Pure contract tests (no Houdini or GPU needed) | `python -m pytest -c tests/pytest.ini tests/pytest_cases -m pure` |
| Test catalogue is current (after adding or moving a test) | `python scripts/catalogue-tests.py --check` |
| Build | `./scripts/bootstrap.ps1` once, then `./scripts/build.ps1` |
| Fast native tests (CPU, needs a build and its Houdini 22.0 build) | `./scripts/run-tests.ps1 -Tier fast -BuildDirectory <build> -Artifacts <folder>` |
| Engine patches unchanged or correctly added | `python scripts/engine-fork.py verify` and `python scripts/patch-inventory.py --check` |
| Documents name only paths and options that exist | `python scripts/check-doc-paths.py` |

Which tests apply to which kind of change is listed in the developer guide,
[which tests for which change](docs/dev/README.md#which-tests-for-which-change). Do not add user folders, drive
letters of your computer, e-mail addresses or private scene names to tracked files. Tests are never
loosened, skipped or deleted to make a change pass. If you believe a test is wrong, say why in its own change.

## Licence of contributions

Contributions use the same licence as the code they change or extend. The Houdini integration and most project
tooling use the [Apache License 2.0](LICENSE). The shipped [Blender component](blender/README.md) and the six
Blender fixture generators listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) use the
[GNU General Public License version 3 or later](blender/LICENSE). There is no separate contributor licence
agreement. Contribute work you have the right to share under the applicable licence.

Put the corresponding SPDX identifier in new source files using the file's comment syntax. Use `GPL-3.0-or-later`
for Python sources in `blender/` and for the listed fixture generators. Use `Apache-2.0` for other project
code. Keep the original copyright and licence information on third-party work you adapt.

## Licence hygiene and provenance

- **Do not copy from Blender's GPL source.** Write the integration from documented behaviour and the public APIs.
  The Blender files named in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) as sources of past rewrites carry
  GPL notices. Use the licence on the specific upstream file when verifying provenance. Blender's Apache-2.0
  Cycles sources are in `intern/cycles`.
- **Code that reproduces another program's behaviour needs a provenance record.** If your change makes this project
  match how Blender (or another program) behaves, write it from that program's user documentation, its public API
  documentation, measured results and standard mathematics, and add a behaviour description under
  `docs/dev/provenance/`: what the code must do, which tests pin it, and a "Provenance" section that says what you
  used and what you did not look at. The existing documents in that folder show the format.
- **Code adapted from Apache-2.0 sources** (such as Cycles itself) keeps its original copyright and licence lines,
  and is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **New dependencies, icons or data** need their licence recorded in `THIRD_PARTY_NOTICES.md` and their licence text
  under `packaging/notices/`.

## Sharing your own build or installer

The project publishes source only. `START HERE - Build Cycles for Houdini.cmd` makes an installer of your build for
each of your Houdini builds, for your own use. If you share an installer, give it your own name and build identity.
The Apache and GPL terms apply to the respective project components. Third-party components, including NVIDIA's,
carry their own redistribution conditions. Whoever distributes the installer is responsible for meeting those
conditions. See [docs/user/build-it-yourself.md](docs/user/build-it-yourself.md) (Part 2),
[docs/dev/nvidia-licences.md](docs/dev/nvidia-licences.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is
a description, not legal advice.
