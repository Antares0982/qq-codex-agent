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
pkgs.runCommand "qq-codex-deployment-check"
  {
    nativeBuildInputs = [
      pkgs.python313
      pkgs.ripgrep
    ];
  }
  ''
    python - ${configFile} ${requirementsFile} <<'PY'
    import sys
    import subprocess
    import tempfile
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
    root = Path("/var/lib/qq-codex-agent")
    for parent, allowed in (
        (root, ("codex",)),
        (root / "codex", ("tmp", "skills", "plugins")),
        (root / "codex/tmp", ("arg0",)),
        (root / "codex/plugins", ("cache",)),
    ):
        names = ("auth.json", ".secret", "sessions", "memories", "generated_images", "state_5.sqlite") + tuple(
            component[:i] + suffix
            for component in allowed
            for i in range(len(component) + 1)
            for suffix in ("", "X", ".")
        )
        for name in names:
            if name in ("", ".", *allowed):
                continue
            for path in (parent / name, parent / name / "nested/secret"):
                assert any(
                    ancestor.full_match(pattern)
                    for ancestor in (path, *path.parents)
                    for pattern in patterns
                ), path
        for name in allowed:
            paths = [parent / name]
            if name in ("skills", "cache", "arg0"):
                paths.append(parent / name / "nested/resource")
            for path in paths:
                assert not any(
                    ancestor.full_match(pattern)
                    for ancestor in (path, *path.parents)
                    for pattern in patterns
                ), path
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        denied = [
            "canary", "codex/auth.json", "codex/sessions/history.jsonl",
            "codex/tmp/canary", "codex/plugins/canary",
            "codex/skillsextra/nested", "codex/plugins/cacheextra/nested",
        ]
        readable = [
            "codex/skills/.system/imagegen/SKILL.md",
            "codex/skills/.system/imagegen/references/prompting.md",
            "codex/plugins/cache/plugin/skills/example/SKILL.md",
            "codex/plugins/cache/probe", "codex/tmp/arg0/helper",
        ]
        for name in denied + readable:
            path = base / "qq-codex-agent" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("probe")
        groups = {}
        for pattern in patterns:
            pattern = pattern.replace("/var/lib", directory, 1)
            glob = next((i for i, char in enumerate(pattern) if char in "*?["), None)
            if glob is None:
                continue
            split = pattern.rfind("/", 0, glob)
            groups.setdefault(pattern[:split], []).append(pattern[split + 1:])
        masked = set()
        for search_root, globs in groups.items():
            result = subprocess.run(
                ["rg", "--files", "--hidden", "--no-ignore", "--null"]
                + [arg for glob in globs for arg in ("--glob", glob)]
                + ["--", search_root], capture_output=True, check=False,
            )
            assert result.returncode in (0, 1), result.stderr
            masked.update(result.stdout.decode().split("\0"))
        for name in denied:
            assert str(base / "qq-codex-agent" / name) in masked, name
        for name in readable:
            assert str(base / "qq-codex-agent" / name) not in masked, name
    PY
    touch "$out"
  ''
