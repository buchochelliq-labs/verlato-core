// Obtaining the native `verlato` runtime for the extension: the release bundle for this platform,
// downloaded from the GitHub release that matches the extension's version, checked against the
// release's SHA256SUMS and unpacked into the extension's global storage.
//
// Nothing here imports `vscode`, so it can be unit-tested; the extension supplies the fetcher
// (Node's https, which VS Code routes through `http.proxy`) and the progress reporting.

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as https from "node:https";
import * as path from "node:path";
import * as zlib from "node:zlib";

export const RELEASES_URL = "https://github.com/buchochelliq-labs/verlato-core/releases";
export const INSTALL_DOCS_URL = "https://buchochelliq-labs.github.io/verlato-core/getting-started/";

/** Release bundle targets (`rab.yml`, package `verlato-native`), keyed by Node's platform-arch. */
const TARGETS: Record<string, string> = {
  "linux-x64": "x86_64-unknown-linux-gnu",
  "linux-arm64": "aarch64-unknown-linux-gnu",
  "darwin-arm64": "aarch64-apple-darwin",
  "darwin-x64": "x86_64-apple-darwin",
  "win32-x64": "x86_64-pc-windows-msvc",
};

export interface RuntimeBundle {
  target: string;
  /** The top-level directory inside the archive: `verlato-v<version>-<target>`. */
  name: string;
  archive: string;
  url: string;
  sumsUrl: string;
  executable: string;
}

/** The release bundle for a platform, or undefined when Verlato publishes none for it. */
export function runtimeBundle(
  version: string,
  platform: NodeJS.Platform = process.platform,
  arch: string = process.arch,
): RuntimeBundle | undefined {
  const target = TARGETS[`${platform}-${arch}`];
  if (!target) {
    return undefined;
  }
  const name = `verlato-v${version}-${target}`;
  const archive = `${name}${platform === "win32" ? ".zip" : ".tar.gz"}`;
  const base = `${RELEASES_URL}/download/v${version}`;
  return {
    target,
    name,
    archive,
    url: `${base}/${archive}`,
    sumsUrl: `${base}/SHA256SUMS`,
    executable: platform === "win32" ? "verlato.exe" : "verlato",
  };
}

/** Where a downloaded runtime lives: `<storage>/runtime/<version>/<bundle name>/verlato[.exe]`. */
export function downloadedExecutable(storageDir: string, version: string, bundle: RuntimeBundle): string {
  return path.join(storageDir, "runtime", version, bundle.name, bundle.executable);
}

/** `{file name: sha256}` from `sha256sum`-format text (`<hex>  <name>`, optionally `*<name>`). */
export function parseSha256Sums(text: string): Map<string, string> {
  const sums = new Map<string, string>();
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) {
      continue;
    }
    const match = /^([0-9a-fA-F]{64})\s+\*?(.+)$/.exec(line);
    if (match) {
      sums.set(path.posix.basename(match[2].trim()), match[1].toLowerCase());
    }
  }
  return sums;
}

/** Whether `verlato --version` output (`verlato 0.2.0`) has the extension's major.minor. */
export function versionMatches(versionOutput: string, extensionVersion: string): boolean {
  const found = /(\d+)\.(\d+)\.\d+/.exec(versionOutput);
  const wanted = /^(\d+)\.(\d+)\./.exec(extensionVersion);
  return Boolean(found && wanted && found[1] === wanted[1] && found[2] === wanted[2]);
}

/** Candidate `verlato` executables on PATH, in order (with PATHEXT on Windows). */
export function pathExecutables(
  envPath: string | undefined,
  platform: NodeJS.Platform = process.platform,
  pathExt: string | undefined = process.env.PATHEXT,
): string[] {
  const separator = platform === "win32" ? ";" : ":";
  const extensions =
    platform === "win32" ? (pathExt || ".EXE;.CMD;.BAT").split(";").filter(Boolean).map((e) => e.toLowerCase()) : [""];
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  const candidates: string[] = [];
  for (const dir of (envPath || "").split(separator)) {
    const trimmed = dir.trim().replace(/^"(.*)"$/, "$1");
    if (!trimmed) {
      continue;
    }
    for (const extension of extensions) {
      candidates.push(join(trimmed, `verlato${extension}`));
    }
  }
  return candidates;
}

export type Fetcher = (url: string, onProgress?: (received: number, total: number | undefined) => void) => Promise<Buffer>;

/** GET `url` with Node's https, following redirects (release assets redirect to a CDN). */
export const httpsFetch: Fetcher = (url, onProgress) =>
  new Promise((resolve, reject) => {
    const get = (current: string, redirects: number) => {
      const request = https.get(current, { headers: { "User-Agent": "verlato-vscode" } }, (response) => {
        const status = response.statusCode ?? 0;
        if (status >= 300 && status < 400 && response.headers.location) {
          response.resume();
          if (redirects >= 10) {
            reject(new Error(`Too many redirects fetching ${url}`));
            return;
          }
          get(new URL(response.headers.location, current).toString(), redirects + 1);
          return;
        }
        if (status !== 200) {
          response.resume();
          reject(new Error(`HTTP ${status} fetching ${current}`));
          return;
        }
        const total = Number(response.headers["content-length"]) || undefined;
        const chunks: Buffer[] = [];
        let received = 0;
        response.on("data", (chunk: Buffer) => {
          chunks.push(chunk);
          received += chunk.length;
          onProgress?.(received, total);
        });
        response.on("end", () => resolve(Buffer.concat(chunks)));
        response.on("error", reject);
      });
      request.on("error", reject);
      request.setTimeout(60_000, () => request.destroy(new Error(`Timed out fetching ${current}`)));
    };
    get(url, 0);
  });

export interface InstallOptions {
  version: string;
  storageDir: string;
  platform?: NodeJS.Platform;
  arch?: string;
  fetch?: Fetcher;
  onProgress?: (message: string, fraction?: number) => void;
}

/**
 * Download, verify and unpack the runtime for this platform; returns the executable's path.
 * The archive is checked against the release's SHA256SUMS before anything is written, unpacked
 * into a temporary directory and then moved into place, so a failed install leaves nothing behind.
 */
export async function installRuntime(options: InstallOptions): Promise<string> {
  const platform = options.platform ?? process.platform;
  const bundle = runtimeBundle(options.version, platform, options.arch ?? process.arch);
  if (!bundle) {
    throw new UnsupportedPlatformError(platform, options.arch ?? process.arch);
  }
  const fetch = options.fetch ?? httpsFetch;
  const report = options.onProgress ?? (() => undefined);

  report(`Checking the Verlato ${options.version} release`);
  const sums = parseSha256Sums((await fetch(bundle.sumsUrl)).toString("utf8"));
  const expected = sums.get(bundle.archive);
  if (!expected) {
    throw new Error(`The v${options.version} release's SHA256SUMS lists no ${bundle.archive}.`);
  }

  report(`Downloading ${bundle.archive}`, 0);
  const data = await fetch(bundle.url, (received, total) => {
    if (total) {
      report(`Downloading ${bundle.archive} (${Math.round(received / 1e6)} of ${Math.round(total / 1e6)} MB)`, received / total);
    }
  });
  const actual = crypto.createHash("sha256").update(data).digest("hex");
  if (actual !== expected) {
    throw new Error(`Checksum mismatch for ${bundle.archive}: expected ${expected}, got ${actual}. Nothing was installed.`);
  }

  report(`Unpacking ${bundle.archive}`);
  const versionDir = path.join(options.storageDir, "runtime", options.version);
  fs.mkdirSync(versionDir, { recursive: true });
  const staging = fs.mkdtempSync(path.join(versionDir, ".staging-"));
  try {
    const entries = bundle.archive.endsWith(".zip") ? readZip(data) : readTarGz(data);
    writeEntries(entries, staging);
    const unpacked = path.join(staging, bundle.name);
    const executable = path.join(unpacked, bundle.executable);
    if (!fs.existsSync(executable) || !fs.existsSync(path.join(unpacked, "wasm", "parser_manifest.json"))) {
      throw new Error(`${bundle.archive} does not contain ${bundle.name}/${bundle.executable} and its parser bundle.`);
    }
    if (platform !== "win32") {
      fs.chmodSync(executable, 0o755);
    }
    const final = path.join(versionDir, bundle.name);
    fs.rmSync(final, { recursive: true, force: true });
    fs.renameSync(unpacked, final);
    return path.join(final, bundle.executable);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

export class UnsupportedPlatformError extends Error {
  constructor(platform: string, arch: string) {
    super(`Verlato publishes no runtime for ${platform}-${arch}.`);
  }
}

export interface ArchiveEntry {
  name: string;
  data?: Buffer;
  directory: boolean;
  mode?: number;
}

/** The regular files and directories of a gzipped tar (ustar, pax and GNU long names). */
export function readTarGz(data: Buffer): ArchiveEntry[] {
  const tar = zlib.gunzipSync(data);
  const entries: ArchiveEntry[] = [];
  let offset = 0;
  let longName: string | undefined;
  let paxPath: string | undefined;
  const field = (start: number, length: number) => tar.subarray(start, start + length).toString("utf8").replace(/\0.*$/s, "");
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) {
      break;
    }
    const size = parseInt(field(offset + 124, 12).trim() || "0", 8);
    const type = String.fromCharCode(header[156] || 48);
    const prefix = field(offset + 345, 155);
    let name = field(offset, 100);
    if (prefix) {
      name = `${prefix}/${name}`;
    }
    const mode = parseInt(field(offset + 100, 8).trim() || "0", 8);
    const body = tar.subarray(offset + 512, offset + 512 + size);
    offset += 512 + Math.ceil(size / 512) * 512;
    if (type === "L") {
      longName = body.toString("utf8").replace(/\0.*$/s, "");
      continue;
    }
    if (type === "x") {
      paxPath = parsePaxPath(body) ?? paxPath;
      continue;
    }
    if (type === "g") {
      continue;
    }
    name = paxPath ?? longName ?? name;
    paxPath = undefined;
    longName = undefined;
    if (type === "5") {
      entries.push({ name, directory: true });
    } else if (type === "0" || type === "7") {
      entries.push({ name, data: Buffer.from(body), directory: false, mode });
    }
  }
  return entries;
}

function parsePaxPath(body: Buffer): string | undefined {
  let found: string | undefined;
  let offset = 0;
  const text = body.toString("utf8");
  while (offset < text.length) {
    const space = text.indexOf(" ", offset);
    if (space < 0) {
      break;
    }
    const length = parseInt(text.slice(offset, space), 10);
    if (!length) {
      break;
    }
    const record = text.slice(space + 1, offset + length - 1);
    const equals = record.indexOf("=");
    if (record.slice(0, equals) === "path") {
      found = record.slice(equals + 1);
    }
    offset += length;
  }
  return found;
}

/** The entries of a zip archive (stored or deflated, from the central directory). */
export function readZip(data: Buffer): ArchiveEntry[] {
  let end = -1;
  for (let i = data.length - 22; i >= Math.max(0, data.length - 65_557); i--) {
    if (data.readUInt32LE(i) === 0x06054b50) {
      end = i;
      break;
    }
  }
  if (end < 0) {
    throw new Error("Not a zip archive (no end of central directory).");
  }
  const count = data.readUInt16LE(end + 10);
  let offset = data.readUInt32LE(end + 16);
  const entries: ArchiveEntry[] = [];
  for (let i = 0; i < count; i++) {
    if (data.readUInt32LE(offset) !== 0x02014b50) {
      throw new Error("Corrupt zip central directory.");
    }
    const method = data.readUInt16LE(offset + 10);
    const compressedSize = data.readUInt32LE(offset + 20);
    const nameLength = data.readUInt16LE(offset + 28);
    const extraLength = data.readUInt16LE(offset + 30);
    const commentLength = data.readUInt16LE(offset + 32);
    const localOffset = data.readUInt32LE(offset + 42);
    const name = data.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
    offset += 46 + nameLength + extraLength + commentLength;

    if (data.readUInt32LE(localOffset) !== 0x04034b50) {
      throw new Error(`Corrupt zip entry ${name}.`);
    }
    const start = localOffset + 30 + data.readUInt16LE(localOffset + 26) + data.readUInt16LE(localOffset + 28);
    const raw = data.subarray(start, start + compressedSize);
    if (name.endsWith("/")) {
      entries.push({ name, directory: true });
      continue;
    }
    let content: Buffer;
    if (method === 0) {
      content = Buffer.from(raw);
    } else if (method === 8) {
      content = zlib.inflateRawSync(raw);
    } else {
      throw new Error(`Unsupported zip compression method ${method} for ${name}.`);
    }
    entries.push({ name, data: content, directory: false });
  }
  return entries;
}

/** Write archive entries under `root`, refusing any path that would escape it. */
export function writeEntries(entries: ArchiveEntry[], root: string): void {
  const base = path.resolve(root);
  for (const entry of entries) {
    const relative = entry.name.replace(/\\/g, "/").replace(/^\.\//, "");
    if (!relative || relative.startsWith("/") || /^[A-Za-z]:/.test(relative)) {
      throw new Error(`Refusing archive entry with an absolute path: ${entry.name}`);
    }
    const target = path.resolve(base, relative);
    if (target !== base && !target.startsWith(base + path.sep)) {
      throw new Error(`Refusing archive entry outside the target directory: ${entry.name}`);
    }
    if (entry.directory) {
      fs.mkdirSync(target, { recursive: true });
      continue;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, entry.data ?? Buffer.alloc(0));
    if (entry.mode && process.platform !== "win32") {
      fs.chmodSync(target, entry.mode & 0o777);
    }
  }
}
