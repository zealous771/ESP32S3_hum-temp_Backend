/// <reference types="vite/client" />

// 这个文件的作用只有一个：让 TypeScript 认识 `import.meta.env` 之类的
// Vite 运行时 API，以及 `*.vue` 的模块声明。
//
// ★ 为什么不写 `declare module '*.vue'` 那段常见的 shim：
//
//   那段 shim 把每个 .vue 都声明成 `DefineComponent<{}, {}, any>`，
//   于是**组件 props 的类型检查全部失效**——传错 props 不报错，
//   只是运行期拿不到值。
//
//   vue-tsc 本身就能理解 .vue 文件，不需要 shim。
//   网上那些模板里的 shim 是给"只用 tsc 不用 vue-tsc"的项目准备的，
//   在本项目里加上它反而会把类型安全关掉。
