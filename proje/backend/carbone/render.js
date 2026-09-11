"use strict";

/**
 * Bounded, single-shot Carbone render CLI.
 *
 * Invoked by backend/carbone_render.py as a short-lived subprocess -- one
 * process per artifact, never a long-running server. All three arguments
 * are file PATHS, never inline data or the CLI arguments themselves, so a
 * large dataset never has to fit on a command line or be re-typed by a
 * model: the data always comes from a JSON file written server-side.
 *
 * Usage:
 *   node render.js --template <path> --data <path.json> --output <path>
 *
 * Exit codes:
 *   0  rendered successfully, output written
 *   1  bad arguments
 *   2  template not found / not readable
 *   3  data file not found / not valid JSON
 *   4  Carbone render error
 *   5  could not write output
 *
 * stdout: nothing on success. stderr: a single-line, bounded error message
 * on failure. backend/carbone_render.py treats these exit codes and stderr
 * as the whole contract -- it does not parse stdout.
 */

const fs = require("fs");
const path = require("path");

function fail(code, message) {
  process.stderr.write(String(message).slice(0, 4000) + "\n");
  process.exit(code);
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--template" || arg === "--data" || arg === "--output") {
      out[arg.slice(2)] = argv[i + 1];
      i += 1;
    }
  }
  return out;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.template || !args.data || !args.output) {
    fail(1, "usage: render.js --template <path> --data <path.json> --output <path>");
    return;
  }

  const templatePath = path.resolve(args.template);
  const dataPath = path.resolve(args.data);
  const outputPath = path.resolve(args.output);

  if (!fs.existsSync(templatePath) || !fs.statSync(templatePath).isFile()) {
    fail(2, `template not found: ${templatePath}`);
    return;
  }

  let carbone;
  try {
    carbone = require("carbone");
  } catch (err) {
    fail(2, `carbone runtime is not installed (${err.message})`);
    return;
  }

  let data;
  try {
    data = JSON.parse(fs.readFileSync(dataPath, "utf8"));
  } catch (err) {
    fail(3, `data file is not valid JSON: ${err.message}`);
    return;
  }

  carbone.render(templatePath, data, {}, (err, result) => {
    if (err) {
      fail(4, `carbone render failed: ${err.message || err}`);
      return;
    }
    try {
      fs.writeFileSync(outputPath, result);
    } catch (writeErr) {
      fail(5, `could not write output: ${writeErr.message}`);
      return;
    }
    process.exit(0);
  });
}

main();
