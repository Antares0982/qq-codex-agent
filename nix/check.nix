{ nixpkgs }:
let
  pkgs = import nixpkgs { system = builtins.currentSystem; };
  evaluate =
    extra:
    (import (nixpkgs + "/nixos/lib/eval-config.nix") {
      system = builtins.currentSystem;
      modules = [
        ./module.nix
        {
          services.qq-codex-agent = {
            enable = true;
            package = pkgs.emptyDirectory;
            allowlistFile = "/run/test-allowlist";
            tokenFile = "/run/test-token";
          };
        }
        extra
      ];
    }).config;
  config = evaluate { };
  custom = evaluate {
    services.qq-codex-agent = {
      agentsFile = "/custom/common.md";
      privateAgentsFile = "/custom/private.md";
      groupAgentsFile = "/custom/group.md";
    };
  };
  mounts = config.systemd.services.qq-codex-agent.serviceConfig.BindReadOnlyPaths;
  customMounts = custom.systemd.services.qq-codex-agent.serviceConfig.BindReadOnlyPaths;
  promptNames = [
    "AGENTS.md"
    "AGENTS.private.md"
    "AGENTS.group.md"
  ];
  configMount = builtins.head (
    builtins.filter (path: pkgs.lib.hasSuffix ":/etc/qq-codex-agent/config.toml" path) mounts
  );
  configFile = builtins.head (pkgs.lib.splitString ":" configMount);
in
assert builtins.all (
  name:
  builtins.elem "${pkgs.emptyDirectory}/share/qq-codex-agent/${name}:/etc/qq-codex-agent/${name}" mounts
) promptNames;
assert builtins.all (path: builtins.elem path customMounts) [
  "/custom/common.md:/etc/qq-codex-agent/AGENTS.md"
  "/custom/common.md:/var/lib/qq-codex-agent/codex/AGENTS.md"
  "/custom/private.md:/etc/qq-codex-agent/AGENTS.private.md"
  "/custom/group.md:/etc/qq-codex-agent/AGENTS.group.md"
];
assert !(builtins.elem "/nix/store" mounts);
assert !(builtins.elem "/var/lib/qq-codex-agent/codex/tmp/arg0" mounts);
assert builtins.elem "/run/test-token:/etc/qq-codex-agent/napcat-token" mounts;
assert !(builtins.any (rule: pkgs.lib.hasPrefix "C " rule) config.systemd.tmpfiles.rules);
pkgs.runCommand "qq-codex-deployment-check" { nativeBuildInputs = [ pkgs.python313 ]; } ''
  python - ${configFile} <<'PY'
  import sys
  import tomllib
  from pathlib import Path

  config = tomllib.loads(Path(sys.argv[1]).read_text())
  for key, name in (
      ("agents_file", "AGENTS.md"),
      ("private_agents_file", "AGENTS.private.md"),
      ("group_agents_file", "AGENTS.group.md"),
  ):
      assert config[key] == f"/etc/qq-codex-agent/{name}"
  PY
  touch "$out"
''
