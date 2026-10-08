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
      groupPromptAllowMembers = true;
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
assert !config.services.qq-codex-agent.groupPromptAllowMembers;
assert custom.services.qq-codex-agent.groupPromptAllowMembers;
assert builtins.all (
  name:
  builtins.elem "${pkgs.emptyDirectory}/share/qq-codex-agent/${name}:/etc/qq-codex-agent/${name}" mounts
) promptNames;
assert builtins.all (path: builtins.elem path customMounts) [
  "/custom/common.md:/etc/qq-codex-agent/AGENTS.md"
  "/custom/private.md:/etc/qq-codex-agent/AGENTS.private.md"
  "/custom/group.md:/etc/qq-codex-agent/AGENTS.group.md"
];
assert builtins.elem "/run/codex-auth" mounts;
assert builtins.elem "codex-auth.service" config.systemd.services.qq-codex-agent.requires;
assert !(builtins.elem "/nix/store" mounts);
assert builtins.elem "${pkgs.emptyDirectory}:/var/lib/qq-codex-agent/codex/tmp/arg0" mounts;
assert builtins.elem "/run/test-token:/etc/qq-codex-private/napcat-token" mounts;
assert !(builtins.any (rule: pkgs.lib.hasPrefix "C " rule) config.systemd.tmpfiles.rules);
pkgs.runCommand "qq-codex-deployment-check"
  {
    nativeBuildInputs = [
      pkgs.python313
    ];
  }
  ''
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
    assert config["auth_socket"] == "/run/codex-auth/auth.sock"
    assert config["group_prompt_allow_members"] is False
    requirements = tomllib.loads(Path(sys.argv[2]).read_text())
    assert requirements["allowed_approval_policies"] == ["on-request"]
    assert requirements["allowed_approvals_reviewers"] == ["auto_review"]
    patterns = requirements["permissions"]["filesystem"]["deny_read"]
    assert patterns == ["/var/lib/qq-codex-agent", "/etc/qq-codex-private", "/run/codex-auth"]
    assert config["resources_dir"] == "/var/lib/qq-codex-resources"
    assert config["token_file"] == "/etc/qq-codex-private/napcat-token"
    assert config["allowlist_file"] == "/etc/qq-codex-private/allowlist.toml"
    PY
    touch "$out"
  ''
