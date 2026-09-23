{ inputs, system }:
let
  pkgs = inputs.nixpkgs.legacyPackages.${system};
  source = ../.;
  workspace = inputs.uv2nix.lib.workspace.loadWorkspace { workspaceRoot = source; };
  pythonSet =
    (pkgs.callPackage inputs.pyproject-nix.build.packages {
      python = pkgs.python313;
    }).overrideScope
      (
        pkgs.lib.composeManyExtensions [
          inputs.pyproject-build-systems.overlays.default
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
          (_final: prev: {
            openai-codex-cli-bin = prev.openai-codex-cli-bin.overrideAttrs (old: {
              buildInputs = (old.buildInputs or [ ]) ++ [ pkgs.ncurses ];
            });
          })
        ]
      );
  env = pythonSet.mkVirtualEnv "qq-codex-agent-env" workspace.deps.default;
in
pkgs.runCommand "qq-codex-agent" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
  mkdir -p "$out/bin" "$out/share/qq-codex-agent"
  makeWrapper ${env}/bin/qq-codex-agent "$out/bin/qq-codex-agent"
  makeWrapper ${env}/bin/python "$out/bin/qq-codex-check" \
    --add-flags ${source}/check_sandbox.py
  makeWrapper ${env}/bin/python "$out/bin/qq-codex-python"
  cp ${source}/AGENTS.runtime.md "$out/share/qq-codex-agent/AGENTS.md"
  cp ${source}/AGENTS.private.runtime.md "$out/share/qq-codex-agent/AGENTS.private.md"
  cp ${source}/AGENTS.group.runtime.md "$out/share/qq-codex-agent/AGENTS.group.md"
  for prompt in "$out/share/qq-codex-agent/"*.md; do
    test -s "$prompt"
  done
  "$out/bin/qq-codex-agent" --help >/dev/null
  ${env}/bin/python -m unittest discover -s ${source}
''
