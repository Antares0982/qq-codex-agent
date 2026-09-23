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
  requirementsMount = builtins.head (
    builtins.filter (path: pkgs.lib.hasSuffix ":/etc/codex/requirements.toml" path) mounts
  );
  requirementsFile = builtins.head (pkgs.lib.splitString ":" requirementsMount);
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
  python - ${configFile} ${requirementsFile} <<'PY'
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
  requirements = tomllib.loads(Path(sys.argv[2]).read_text())
  patterns = requirements["permissions"]["filesystem"]["deny_read"]
  parent = Path("/var/lib/qq-codex-agent")
  for component in ("codex", "tmp", "arg0"):
      for name in ("auth.json", ".secret", component + "extra") + tuple(
          component[:i] + suffix
          for i in range(len(component))
          for suffix in ("", "X", ".")
          if component[:i] + suffix not in ("", ".")
      ):
          for path in (parent / name, parent / name / "nested/secret"):
              assert any(
                  ancestor.full_match(pattern)
                  for ancestor in (path, *path.parents)
                  for pattern in patterns
              ), path
      parent /= component
  for path in (parent, parent / "codex-arg0-test/codex-execve-wrapper"):
      assert not any(path.full_match(pattern) for pattern in patterns), path
  PY
  touch "$out"
''
