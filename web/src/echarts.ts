/**
 * ECharts 的**按需引入**。
 *
 * ★ 为什么不用 `import * as echarts from 'echarts'`：
 *
 *   那个写法会把整个 ECharts 打进产物——包括地图、雷达、树图、桑基图、
 *   WebGL 渲染器、以及几十种我们用不到的组件。压缩后约 1MB。
 *
 *   按需引入之后只保留"折线图 + 网格 + 提示框 + 缩放 + 标线 + Canvas 渲染"，
 *   体积降到约 300KB。对局域网内的工具来说这个差别不影响功能，
 *   但影响**首屏时间**和构建产物的大小——而这个项目是要长期跑的，
 *   没必要为用不到的能力付这笔钱。
 *
 *   代价：加了新图表类型时要回来在这里补一行 `echarts.use([...])`，
 *   忘了的症状是"图表不显示但也不报错"（ECharts 找不到对应的
 *   series 类型时会静默不画）。所以下面把用到的都列全并注明用途。
 */

import * as echarts from 'echarts/core'

// 图表类型：只用折线（温度/湿度曲线）
import { LineChart } from 'echarts/charts'

// 组件：坐标轴网格、提示框、缩放、标线（用来画"现在"的竖线）
import {
  DataZoomComponent,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'

// 渲染器：Canvas（默认，够用且兼容性最好；SVG 渲染器在数据点多时更慢）
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  MarkLineComponent,
  CanvasRenderer,
])

/**
 * ECharts 实例的类型。
 *
 * 用 `ReturnType<typeof init>` 而不是 `import type { EChartsType }`：
 * 那个类型名的导出位置在 ECharts 5/6 之间变过，从函数签名反推
 * 不会因为版本升级而失效。
 */
export type EChartsInstance = ReturnType<typeof echarts.init>

export default echarts
