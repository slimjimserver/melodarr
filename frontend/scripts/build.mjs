import { readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { brotliCompressSync, constants, gzipSync } from "node:zlib";
import { build } from "esbuild";

const staticDirectory = "static";
const compressionMinimumBytes = 1024;
const compressibleExtensions = new Set([
  ".css",
  ".html",
  ".js",
  ".json",
  ".svg",
  ".webmanifest",
]);

await build({
  entryPoints: ["src/theme.ts", "src/app.ts", "src/discovery.ts", "src/service-worker.ts", "src/style.css"],
  outdir: staticDirectory,
  bundle: false,
  legalComments: "none",
  minify: true,
  sourcemap: false,
  target: "es2022",
});

await build({
  entryPoints: ["src/rooms.ts"],
  outdir: staticDirectory,
  bundle: true,
  format: "esm",
  banner: { js: "/*! qrcode-generator 2.0.4 · Copyright (c) 2009 Kazuhiko Arase · MIT · /static/third-party-licenses.txt */" },
  legalComments: "none",
  minify: true,
  sourcemap: false,
  target: "es2022",
});

const initialFiles = await readdir(staticDirectory);
await Promise.all(
  initialFiles
    .filter((name) => name.endsWith(".map") || name.endsWith(".gz") || name.endsWith(".br"))
    .map((name) => rm(join(staticDirectory, name), { force: true })),
);

const files = await readdir(staticDirectory);
await Promise.all(files.map(async (name) => {
  const path = join(staticDirectory, name);
  const metadata = await stat(path);
  if (
    !metadata.isFile()
    || metadata.size < compressionMinimumBytes
    || !compressibleExtensions.has(extname(name))
  ) return;

  const contents = await readFile(path);
  await Promise.all([
    writeFile(`${path}.gz`, gzipSync(contents, { level: 9 })),
    writeFile(`${path}.br`, brotliCompressSync(contents, {
      params: {
        [constants.BROTLI_PARAM_QUALITY]: 11,
      },
    })),
  ]);
}));
