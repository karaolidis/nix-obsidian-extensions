_: {
  projectRootFile = "flake.nix";

  programs = {
    nixfmt = {
      enable = true;
      strict = true;
    };

    statix.enable = true;

    ruff = {
      format = true;
      check = true;
    };

    jsonfmt.enable = true;
    yamlfmt.enable = true;
  };

  settings = {
    global.excludes = [ ".envrc" ];

    formatter = {
      ruff-check.options = [
        "--line-length"
        "79"
      ];
      ruff-format.options = [
        "--line-length"
        "79"
      ];
    };
  };
}
