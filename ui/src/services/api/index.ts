// Every call to the manager's API, each a function over `request`, and the shapes it answers with, in a module for
// each resource. Everything imports from here (`@/services/api`), never a module, so that a test's mock of it reaches
// every caller.
export * from './applications'
export * from './backups'
export * from './info'
export * from './logs'
export * from './metrics'
export * from './notifications'
export * from './projects'
export * from './requests'
export * from './settings'
export * from './tasks'
export * from './workers'
