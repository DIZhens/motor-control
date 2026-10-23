#ifndef G431_CONTROL_PROFILE_H
#define G431_CONTROL_PROFILE_H

#include "control_runtime.h"

/* 正式 Run 的链接期配置；Observe 的关断装配仍在其入口内独立定义。 */
extern const ControlRuntimeConfig g431_control_config;

/* 只在启动 Drive Timing 前调用一次，初始化配置绑定的私有 CORDIC 上下文。
 * 不修改配置、控制器历史、Motor 或功率状态。运行期仍由原实时所有者调用后端。 */
bool g431_control_profile_initialize_math(void);

#endif
