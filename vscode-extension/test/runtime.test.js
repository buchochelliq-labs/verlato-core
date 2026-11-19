const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const zlib = require("node:zlib");

const {
  downloadedExecutable,
  installRuntime,
  parseSha256Sums,
  pathExecutables,
  readTarGz,
  readZip,
  runtimeBundle,
  UnsupportedPlatformError,
  versionMatches,
  writeEntries,
  runtimeVersion,
} = require("../dist/client/runtime");

// -- tiny archive writers for fixtures ---------------------------------------------------------

function tarHeader(name, size, type, mode) {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, "utf8");
  header.write(mode.toString(8).padStart(7, "0") + "\0", 100);
  header.write("0000000\0", 108);
  header.write("0000000\0", 116);
  header.write(size.toString(8).padStart(11, "0") + "\0", 124);
  header.write("00000000000\0", 136);
  header.write("        ", 148);
  header.write(type, 156);
  header.write("ustar\0", 257);
  header.write("00", 263);
  let sum = 0;
  for (const byte of header) sum += byte;
  header.write(sum.toString(8).padStart(6, "0") + "\0 ", 148);
  return header;
}

function tarGz(files) {
  const parts = [];
  for (const [name, content, mode = 0o644] of files) {
    if (content === null) {
      parts.push(tarHeader(name, 0, "5", 0o755));
      continue;
    }
    const data = Buffer.from(content);
    if (name.length > 99) {
      const longName = Buffer.from(name + "\0");
      parts.push(tarHeader("././@LongLink", longName.length, "L", 0o644), longName, Buffer.alloc((512 - (longName.length % 512)) % 512));
    }
    parts.push(tarHeader(name.slice(0, 99), data.length, "0", mode), data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  parts.push(Buffer.alloc(1024));
  return zlib.gzipSync(Buffer.concat(parts));
}

function zip(files) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const [name, content] of files) {
    const nameBytes = Buffer.from(name);
    const data = Buffer.from(content);
    const compressed = zlib.deflateRawSync(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(8, 8);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(8, 10);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, compressed);
    centrals.push(central, nameBytes);
    offset += local.length + nameBytes.length + compressed.length;
  }
  const centralDir = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralDir.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralDir, end]);
}

function sha256(data) {
  return crypto.createHash("sha256").update(data).digest("hex");
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "verlato-runtime-test-"));
}

function bundleFiles(name, exe) {
  return [
    [`${name}/${exe}`, "#!/bin/sh\necho verlato 0.2.0\n", 0o755],
    [`${name}/wasm/parser_manifest.json`, "{}"],
    [`${name}/wasm/python_parser.wasm`, "\0asm"],
    [`${name}/LICENSE`, "Apache-2.0"],
  ];
}

// -- tests -------------------------------------------------------------------------------------

test("runtime bundles match the release's targets and asset names", () => {
  const linux = runtimeBundle("0.2.0", "linux", "x64");
  assert.equal(linux.target, "x86_64-unknown-linux-gnu");
  assert.equal(linux.archive, "verlato-v0.2.0-x86_64-unknown-linux-gnu.tar.gz");
  assert.equal(
    linux.url,
    "https://github.com/buchochelliq-labs/verlato-core/releases/download/v0.2.0/verlato-v0.2.0-x86_64-unknown-linux-gnu.tar.gz",
  );
  assert.equal(linux.sumsUrl, "https://github.com/buchochelliq-labs/verlato-core/releases/download/v0.2.0/SHA256SUMS");
  assert.equal(linux.executable, "verlato");
  assert.equal(runtimeBundle("0.2.0", "linux", "arm64").target, "aarch64-unknown-linux-gnu");
  assert.equal(runtimeBundle("0.2.0", "darwin", "arm64").target, "aarch64-apple-darwin");
  assert.equal(runtimeBundle("0.2.0", "darwin", "x64").target, "x86_64-apple-darwin");
  const windows = runtimeBundle("0.2.0", "win32", "x64");
  assert.equal(windows.archive, "verlato-v0.2.0-x86_64-pc-windows-msvc.zip");
  assert.equal(windows.executable, "verlato.exe");
  assert.equal(runtimeBundle("0.2.0", "win32", "arm64"), undefined);
  assert.equal(runtimeBundle("0.2.0", "freebsd", "x64"), undefined);
});

test("downloaded runtimes are stored per version", () => {
  const bundle = runtimeBundle("0.2.0", "linux", "x64");
  assert.equal(
    downloadedExecutable("/storage", "0.2.0", bundle),
    path.join("/storage", "runtime", "0.2.0", "verlato-v0.2.0-x86_64-unknown-linux-gnu", "verlato"),
  );
});

test("SHA256SUMS lines are parsed in sha256sum format", () => {
  const a = "a".repeat(64);
  const b = "B".repeat(64);
  const sums = parseSha256Sums(`${a}  verlato-v0.2.0-x.tar.gz\r\n${b} *dist/verlato-v0.2.0-y.zip\n\nnot a line\n`);
  assert.equal(sums.get("verlato-v0.2.0-x.tar.gz"), a);
  assert.equal(sums.get("verlato-v0.2.0-y.zip"), "b".repeat(64));
  assert.equal(sums.size, 2);
});

test("a PATH runtime must match the extension's major.minor", () => {
  assert.equal(versionMatches("verlato 0.2.0\n", "0.2.0"), true);
  assert.equal(versionMatches("verlato 0.2.3", "0.2.0"), true);
  assert.equal(versionMatches("verlato 0.1.9", "0.2.0"), false);
  assert.equal(versionMatches("verlato 1.2.0", "0.2.0"), false);
  assert.equal(versionMatches("command not found", "0.2.0"), false);
});

test("PATH candidates use PATHEXT on Windows", () => {
  assert.deepEqual(pathExecutables("/usr/bin::/opt/v", "linux"), ["/usr/bin/verlato", "/opt/v/verlato"]);
  assert.deepEqual(pathExecutables('C:\\Tools;"C:\\Py\\Scripts"', "win32", ".EXE;.CMD"), [
    "C:\\Tools\\verlato.exe",
    "C:\\Tools\\verlato.cmd",
    "C:\\Py\\Scripts\\verlato.exe",
    "C:\\Py\\Scripts\\verlato.cmd",
  ]);
});

test("tar.gz and zip archives are read, including long names", () => {
  const long = `root/${"d".repeat(120)}/file.txt`;
  const tarEntries = readTarGz(tarGz([["root/", null], ["root/a.txt", "alpha", 0o755], [long, "long"]]));
  assert.deepEqual(
    tarEntries.map((e) => [e.name, e.directory, e.data ? e.data.toString() : null]),
    [["root/", true, null], ["root/a.txt", false, "alpha"], [long, false, "long"]],
  );
  assert.equal(tarEntries[1].mode, 0o755);
  const zipEntries = readZip(zip([["root/a.txt", "alpha"], ["root/b/c.txt", "gamma".repeat(100)]]));
  assert.deepEqual(
    zipEntries.map((e) => [e.name, e.data.toString()]),
    [["root/a.txt", "alpha"], ["root/b/c.txt", "gamma".repeat(100)]],
  );
});

test("archive entries cannot escape the target directory", () => {
  const dir = tempDir();
  for (const name of ["../evil.txt", "a/../../evil.txt", "/etc/evil", "C:/evil"]) {
    assert.throws(() => writeEntries([{ name, data: Buffer.from("x"), directory: false }], dir), /Refusing/);
  }
  assert.equal(fs.existsSync(path.join(dir, "..", "evil.txt")), false);
});

for (const [platform, arch] of [["linux", "x64"], ["win32", "x64"]]) {
  test(`installRuntime downloads, verifies and unpacks the ${platform} bundle`, async () => {
    const bundle = runtimeBundle("0.2.0", platform, arch);
    const files = bundleFiles(bundle.name, bundle.executable);
    const archive = bundle.archive.endsWith(".zip") ? zip(files) : tarGz(files);
    const fetched = [];
    const fetch = async (url) => {
      fetched.push(url);
      if (url === bundle.sumsUrl) return Buffer.from(`${sha256(archive)}  ${bundle.archive}\n${"0".repeat(64)}  other.tar.gz\n`);
      if (url === bundle.url) return archive;
      throw new Error(`unexpected ${url}`);
    };
    const storage = tempDir();
    const messages = [];
    const executable = await installRuntime({
      version: "0.2.0",
      storageDir: storage,
      platform,
      arch,
      fetch,
      onProgress: (m) => messages.push(m),
    });
    assert.equal(executable, downloadedExecutable(storage, "0.2.0", bundle));
    assert.equal(fs.readFileSync(executable, "utf8"), "#!/bin/sh\necho verlato 0.2.0\n");
    assert.ok(fs.existsSync(path.join(path.dirname(executable), "wasm", "parser_manifest.json")));
    if (platform !== "win32" && process.platform !== "win32") {
      assert.equal(fs.statSync(executable).mode & 0o111, 0o111);
    }
    assert.deepEqual(fetched, [bundle.sumsUrl, bundle.url]);
    assert.ok(messages.some((m) => m.startsWith("Downloading")));
    // No staging directories are left behind.
    assert.deepEqual(fs.readdirSync(path.join(storage, "runtime", "0.2.0")), [bundle.name]);
  });
}

test("a checksum mismatch installs nothing", async () => {
  const bundle = runtimeBundle("0.2.0", "linux", "x64");
  const archive = tarGz(bundleFiles(bundle.name, bundle.executable));
  const fetch = async (url) => (url === bundle.sumsUrl ? Buffer.from(`${"f".repeat(64)}  ${bundle.archive}\n`) : archive);
  const storage = tempDir();
  await assert.rejects(installRuntime({ version: "0.2.0", storageDir: storage, platform: "linux", arch: "x64", fetch }), /Checksum mismatch/);
  assert.equal(fs.existsSync(downloadedExecutable(storage, "0.2.0", bundle)), false);
});

test("a release without this platform's archive in SHA256SUMS installs nothing", async () => {
  const bundle = runtimeBundle("0.2.0", "linux", "x64");
  const fetch = async () => Buffer.from(`${"f".repeat(64)}  something-else.tar.gz\n`);
  await assert.rejects(
    installRuntime({ version: "0.2.0", storageDir: tempDir(), platform: "linux", arch: "x64", fetch }),
    new RegExp(`lists no ${bundle.archive.replace(/\./g, "\\.")}`),
  );
});

test("a bundle without the executable or parser bundle is refused", async () => {
  const bundle = runtimeBundle("0.2.0", "linux", "x64");
  const archive = tarGz([[`${bundle.name}/README.md`, "hi"]]);
  const fetch = async (url) => (url === bundle.sumsUrl ? Buffer.from(`${sha256(archive)}  ${bundle.archive}\n`) : archive);
  const storage = tempDir();
  await assert.rejects(installRuntime({ version: "0.2.0", storageDir: storage, platform: "linux", arch: "x64", fetch }), /does not contain/);
  assert.deepEqual(fs.readdirSync(path.join(storage, "runtime", "0.2.0")), []);
});

test("unsupported platforms are reported as such", async () => {
  await assert.rejects(
    installRuntime({ version: "0.2.0", storageDir: tempDir(), platform: "win32", arch: "arm64", fetch: async () => Buffer.alloc(0) }),
    (error) => error instanceof UnsupportedPlatformError,
  );
});

test("the runtime version comes from verlatoRuntime and matches the Rust workspace", () => {
  assert.equal(runtimeVersion({ version: "0.2.0", verlatoRuntime: "0.2.0-beta.1" }), "0.2.0-beta.1");
  assert.equal(runtimeVersion({ version: "0.3.0" }), "0.3.0");
  assert.equal(runtimeVersion({ version: "0.3.0", verlatoRuntime: " " }), "0.3.0");
  assert.equal(runtimeVersion({}), "0.0.0");

  // A pre-release runtime's bundles and tag carry the SemVer suffix.
  const bundle = runtimeBundle("0.2.0-beta.1", "linux", "x64");
  assert.equal(bundle.archive, "verlato-v0.2.0-beta.1-x86_64-unknown-linux-gnu.tar.gz");
  assert.equal(
    bundle.sumsUrl,
    "https://github.com/buchochelliq-labs/verlato-core/releases/download/v0.2.0-beta.1/SHA256SUMS",
  );
  assert.equal(versionMatches("verlato 0.2.0-beta.1\n", "0.2.0-beta.1"), true);

  // The extension downloads the runtime this repository releases: verlatoRuntime must equal the
  // Rust workspace version ([workspace.package] in Cargo.toml).
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  const cargo = fs.readFileSync(path.join(__dirname, "..", "..", "Cargo.toml"), "utf8");
  const section = cargo.split(/^\[workspace\.package\]\s*$/m)[1]?.split(/^\[/m)[0] ?? "";
  const workspace = /^version\s*=\s*"([^"]+)"/m.exec(section);
  assert.ok(workspace, "Cargo.toml has a [workspace.package] version");
  assert.equal(runtimeVersion(manifest), workspace[1]);
});
