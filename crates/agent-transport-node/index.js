// Platform-aware native binding loader
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

const platforms = {
  'darwin-arm64': 'agent-transport-darwin-arm64',
  'darwin-x64': 'agent-transport-darwin-x64',
  'linux-arm64': 'agent-transport-linux-arm64-gnu',
  'linux-x64': 'agent-transport-linux-x64-gnu',
  'win32-x64': 'agent-transport-win32-x64-msvc',
  'win32-arm64': 'agent-transport-win32-arm64-msvc',
};

const key = `${process.platform}-${process.arch}`;
const pkg = platforms[key];

if (!pkg) {
  throw new Error(
    `Unsupported platform: ${key}. ` +
    `Supported: ${Object.keys(platforms).join(', ')}`
  );
}

let binding;
try {
  binding = require(pkg);
} catch {
  try {
    binding = require('./agent-transport.node');
  } catch {
    throw new Error(
      `Failed to load native binding for ${key}. ` +
      `Install the package ("npm install agent-transport") or build from source ("npm run build").`
    );
  }
}

export const { SipEndpoint, AudioStreamEndpoint } = binding;
export default binding;
