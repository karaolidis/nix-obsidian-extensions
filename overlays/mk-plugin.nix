{
  lib,
  fetchurl,
  runCommandLocal,
}:
{
  id,
  name ? id,
  repo,
  version,
  files,
  ...
}:
let
  assets = lib.mapAttrsToList (fname: hash: {
    inherit fname;
    drv = fetchurl {
      url = "https://github.com/${repo}/releases/download/${version}/${fname}";
      inherit hash;
      name = "obsidian-plugin-${id}-${version}-${fname}";
    };
  }) files;
in
runCommandLocal "obsidian-plugin-${id}-${version}"
  {
    passthru = {
      manifestId = id;
      inherit version repo;
    };
    meta = {
      description = "Obsidian community plugin: ${name}";
      homepage = "https://github.com/${repo}";
    };
  }
  ''
    mkdir -p "$out"
    ${lib.concatMapStringsSep "\n" ({ fname, drv }: ''cp "${drv}" "$out/${fname}"'') assets}
  ''
