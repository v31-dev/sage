import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// Dates and number formats must not depend on the machine running the tests.
process.env.TZ = 'UTC'
process.env.LC_ALL = 'en_US.UTF-8'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'happy-dom',
      include: ['tests/**/*.test.ts'],
      // The API client waits this long before every call.
      env: { VITE_LOAD_DELAY: '0' },
      restoreMocks: true,
      unstubGlobals: true,
      unstubEnvs: true,
      coverage: {
        provider: 'v8',
        // Every source file counts, also the ones no test imports.
        include: ['src/**/*.{ts,vue}'],
        exclude: ['src/**/*.d.ts', 'src/assets/**'],
        reporter: ['text-summary', 'json-summary', 'lcov'],
        reportsDirectory: 'coverage',
      },
    },
  })
)
