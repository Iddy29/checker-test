import express, { type Express } from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// The server is a set of modules, so there is no __dirname to be had.
// The place of this file is worked out instead.
const here = path.dirname(fileURLToPath(import.meta.url));

export function serveStatic(app: Express) {
  // The client is built into one place and the server looks in another,
  // so both are looked at.
  const places = [
    path.resolve(here, "public"),
    path.resolve(here, "..", "dist", "public"),
  ];
  const distPath = places.find((one) => fs.existsSync(one));
  if (!distPath) {
    throw new Error(
      `Could not find the build directory: ${places[0]}, make sure to build the client first`,
    );
  }

  app.use(express.static(distPath));

  // fall through to index.html if the file doesn't exist
  app.use("/{*path}", (_req, res) => {
    res.sendFile(path.resolve(distPath, "index.html"));
  });
}
