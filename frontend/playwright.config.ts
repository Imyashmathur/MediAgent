import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir:'./tests/e2e',
  fullyParallel:false,
  workers:1,
  retries:0,
  timeout:60_000,
  expect:{timeout:10_000},
  use:{baseURL:process.env.MEDIAGENT_TEST_URL ?? 'http://localhost:3100', channel:process.env.PLAYWRIGHT_CHANNEL || undefined, trace:'off', screenshot:'off', video:'off'},
  reporter:'list',
});
