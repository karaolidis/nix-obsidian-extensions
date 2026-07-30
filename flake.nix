{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs:
    let
      inherit (inputs.nixpkgs) lib;

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      pkgsBySystem = lib.genAttrs systems (
        system:
        import inputs.nixpkgs {
          inherit system;
          overlays = [ inputs.self.overlays.default ];
        }
      );

      treefmtBySystem = lib.mapAttrs (
        _: pkgs: inputs.treefmt-nix.lib.evalModule pkgs ./treefmt.nix
      ) pkgsBySystem;
    in
    {
      overlays.default = import ./overlays;

      legacyPackages = lib.mapAttrs (_: pkgs: {
        inherit (pkgs) obsidianPlugins obsidianThemes;
      }) pkgsBySystem;

      apps = lib.mapAttrs (_: pkgs: {
        update = {
          type = "app";
          program = lib.getExe (pkgs.callPackage ./packages/update { });
        };
      }) pkgsBySystem;

      devShells = lib.mapAttrs (_: pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            gh
            python3
          ];
        };
      }) pkgsBySystem;

      formatter = lib.mapAttrs (_: treefmt: treefmt.config.build.wrapper) treefmtBySystem;

      checks = lib.mapAttrs (
        system: treefmt:
        let
          devShells = lib.mapAttrs' (
            name: lib.nameValuePair "devShell-${name}"
          ) inputs.self.devShells.${system};
        in
        devShells // { formatting = treefmt.config.build.check inputs.self; }
      ) treefmtBySystem;
    };
}
