{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.qq-codex-agent;
  user = "qq-codex-agent";
  state = "/var/lib/qq-codex-agent";
  work = "/var/lib/qq-codex-work";
  home = "${work}/.home";
  app = cfg.package;
  prompts = {
    agents_file = {
      file = cfg.agentsFile;
      name = "AGENTS.md";
    };
    private_agents_file = {
      file = cfg.privateAgentsFile;
      name = "AGENTS.private.md";
    };
    group_agents_file = {
      file = cfg.groupAgentsFile;
      name = "AGENTS.group.md";
    };
  };
  runtime = pkgs.buildEnv {
    name = "qq-codex-runtime";
    paths =
      with pkgs;
      [
        bash
        coreutils
        git
        uv
        ripgrep
        python313
        bubblewrap
        cacert
      ]
      ++ cfg.extraPackages;
  };
  codexConfig = (pkgs.formats.toml { }).generate "qq-codex-codex-config.toml" (
    lib.recursiveUpdate {
      features.shell_snapshot = false;
      sandbox_workspace_write = {
        network_access = true;
        writable_roots = [
          "${work}/.uv"
          home
        ];
      };
    } cfg.codexSettings
  );
  nsswitch = pkgs.writeText "qq-codex-nsswitch.conf" "hosts: files dns\n";
  configFile = (pkgs.formats.toml { }).generate "qq-codex-config.toml" (
    {
      allowlist_file = "/etc/qq-codex-agent/allowlist.toml";
      napcat_url = cfg.napcatUrl;
      token_file = "/etc/qq-codex-agent/napcat-token";
      state_dir = state;
      workspace_dir = work;
      queue_limit = 8;
      task_timeout = 900;
    }
    // lib.mapAttrs (_: prompt: "/etc/qq-codex-agent/${prompt.name}") prompts
  );
  denySiblings =
    parent: names:
    let
      exclude =
        prefix: remaining:
        let
          next = lib.unique (
            map (name: builtins.substring 0 1 name) (builtins.filter (name: name != "") remaining)
          );
        in
        lib.optional (prefix != "" && !(builtins.elem "" remaining)) prefix
        ++ [ (if next == [ ] then "${prefix}?*" else "${prefix}[!${lib.concatStrings next}]*") ]
        ++ lib.concatMap (
          char:
          exclude (prefix + char) (
            map (name: builtins.substring 1 (builtins.stringLength name) name) (
              builtins.filter (lib.hasPrefix char) remaining
            )
          )
        ) next;
      patterns = exclude "" names;
    in
    map (pattern: "${parent}/${pattern}") patterns;
  requirements = (pkgs.formats.toml { }).generate "qq-codex-requirements.toml" {
    allowed_approval_policies = [ "on-request" ];
    allowed_approvals_reviewers = [ "auto_review" ];
    allowed_sandbox_modes = [
      "workspace-write"
      "read-only"
    ];
    allow_login_shell = false;
    permissions.filesystem.deny_read =
      denySiblings state [ "codex" ]
      ++ denySiblings "${state}/codex" [
        "tmp"
        "skills"
        "plugins"
      ]
      ++ denySiblings "${state}/codex/tmp" [ "arg0" ]
      ++ denySiblings "${state}/codex/plugins" [ "cache" ]
      ++ [
        "/etc/qq-codex-agent/napcat-token"
        "/etc/qq-codex-agent/allowlist.toml"
      ]
      ++ cfg.extraDeniedPaths;
  };
  closure = pkgs.closureInfo {
    rootPaths = [
      app
      runtime
    ];
  };
  runtimeMounts = pkgs.runCommand "qq-codex-runtime-mounts" { } ''
    for unit in qq-codex-agent qq-codex-login; do
      directory="$out/lib/systemd/system/$unit.service.d"
      mkdir -p "$directory"
      echo '[Service]' > "$directory/runtime.conf"
      while IFS= read -r path; do
        echo "BindReadOnlyPaths=$path" >> "$directory/runtime.conf"
      done < ${closure}/store-paths
    done
  '';
  serviceConfig = {
    Type = "exec";
    User = user;
    Group = user;
    RootDirectory = "/var/lib/qq-codex-root";
    MountAPIVFS = true;
    WorkingDirectory = work;
    StateDirectory = [
      "qq-codex-agent"
      "qq-codex-work"
    ];
    StateDirectoryMode = "0700";
    UMask = "0077";
    BindReadOnlyPaths = [
      "${configFile}:/etc/qq-codex-agent/config.toml"
      "${cfg.allowlistFile}:/etc/qq-codex-agent/allowlist.toml"
      "${cfg.agentsFile}:${state}/codex/AGENTS.md"
      "${codexConfig}:${state}/codex/config.toml"
      "${requirements}:/etc/codex/requirements.toml"
      "${pkgs.bash}/bin/bash:/bin/sh"
      "${pkgs.coreutils}/bin/env:/usr/bin/env"
      "${nsswitch}:/etc/nsswitch.conf"
      "/etc/resolv.conf:/etc/resolv.conf"
      "/etc/hosts:/etc/hosts"
    ]
    ++ lib.mapAttrsToList (_: prompt: "${prompt.file}:/etc/qq-codex-agent/${prompt.name}") prompts;
    BindPaths = [
      state
      work
    ];
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    NoNewPrivileges = true;
    RestrictSUIDSGID = true;
    RestrictRealtime = true;
    RestrictNamespaces = "user mnt pid net ipc uts cgroup";
    CapabilityBoundingSet = "";
    MemoryHigh = "1G";
    MemoryMax = "2G";
    TasksMax = 256;
    TimeoutStopSec = "30s";
    KillMode = "control-group";
  };
  environment = {
    HOME = home;
    PATH = lib.mkForce "${work}/.uv/bin:${runtime}/bin";
    XDG_CONFIG_HOME = "/tmp/qq-codex-config";
    XDG_CACHE_HOME = "/tmp/qq-codex-cache";
    UV_CACHE_DIR = "${work}/.uv/cache";
    UV_PYTHON_INSTALL_DIR = "${work}/.uv/python";
    UV_PYTHON_BIN_DIR = "${work}/.uv/bin";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  };
in
{
  options.services.qq-codex-agent = {
    enable = lib.mkEnableOption "personal QQ Codex agent";
    package = lib.mkOption { type = lib.types.package; };
    allowlistFile = lib.mkOption { type = lib.types.str; };
    tokenFile = lib.mkOption { type = lib.types.str; };
    extraPackages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
    };
    extraDeniedPaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };
    codexSettings = lib.mkOption {
      type = (pkgs.formats.toml { }).type;
      default = { };
    };
    napcatUrl = lib.mkOption {
      type = lib.types.str;
      default = "ws://127.0.0.1:3001";
    };
    agentsFile = lib.mkOption {
      type = lib.types.str;
      default = "${app}/share/qq-codex-agent/AGENTS.md";
    };
    privateAgentsFile = lib.mkOption {
      type = lib.types.str;
      default = "${app}/share/qq-codex-agent/AGENTS.private.md";
    };
    groupAgentsFile = lib.mkOption {
      type = lib.types.str;
      default = "${app}/share/qq-codex-agent/AGENTS.group.md";
    };
  };
  config = lib.mkIf cfg.enable {
    systemd.packages = [ runtimeMounts ];
    users.groups.${user} = { };
    users.users.${user} = {
      isSystemUser = true;
      group = user;
      home = state;
    };
    systemd.tmpfiles.rules = [
      "d /var/lib/qq-codex-root 0755 root root -"
      "d ${state} 0700 ${user} ${user} -"
      "d ${state}/codex 0700 ${user} ${user} -"
      "d ${state}/codex/tmp/arg0 0700 ${user} ${user} -"
      "d ${work} 0700 ${user} ${user} -"
      "d ${home} 0700 ${user} ${user} -"
      "d ${work}/.uv 0700 ${user} ${user} -"
      "d /etc/qq-codex-agent 0750 root ${user} -"
    ];
    systemd.services.qq-codex-agent = {
      description = "QQ Codex agent";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
      ];
      restartTriggers = [ runtimeMounts ];
      wants = [ "network-online.target" ];
      inherit environment;
      serviceConfig = serviceConfig // {
        ExecStartPre = [
          "${app}/bin/qq-codex-check"
        ];
        ExecStart = "${app}/bin/qq-codex-agent";
        BindReadOnlyPaths = serviceConfig.BindReadOnlyPaths ++ [
          "${cfg.tokenFile}:/etc/qq-codex-agent/napcat-token"
        ];
        Restart = "on-failure";
        RestartSec = "10s";
      };
    };
    systemd.services.qq-codex-login = {
      description = "QQ Codex device login";
      restartTriggers = [ runtimeMounts ];
      conflicts = [ "qq-codex-agent.service" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      inherit environment;
      serviceConfig = serviceConfig // {
        ExecStart = "${app}/bin/qq-codex-agent --login";
        Restart = "no";
      };
    };
  };
}
