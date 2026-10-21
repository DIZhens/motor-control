#ifndef FOC_MATH_H
#define FOC_MATH_H

#include <stdbool.h>

/* 幅值不变的静止坐标；alpha 对准 A 相，正角从 alpha 轴转向 beta 轴。 */
typedef struct {
    float alpha; /* 静止 α 轴分量，单位与输入一致。 */
    float beta; /* 静止 β 轴分量，单位与输入一致。 */
    bool valid; /* false 时数值均为零，不能将无效零值作为测量或控制输出。 */
} FocAlphaBeta;

/* d 轴沿电角度方向，q 轴相对 d 轴正向超前 90 度；单位与输入一致。 */
typedef struct {
    float d; /* 旋转 d 轴分量，单位与输入一致。 */
    float q; /* 旋转 q 轴分量，单位与输入一致。 */
    bool valid; /* false 时不允许使用本次变换结果。 */
} FocDq;

/* 仅为调制计算结果；valid 不代表已授权使能功率或满足采样窗口要求。 */
typedef struct {
    float applied_alpha_v; /* 圆限幅后真正用于调制的 alpha 电压 [V]。 */
    float applied_beta_v; /* 圆限幅后真正用于调制的 beta 电压 [V]。 */
    float duty_a; /* A 相归一化平均 duty，[0, 1]，不表达驱动引脚极性。 */
    float duty_b; /* B 相归一化平均 duty，[0, 1]。 */
    float duty_c; /* C 相归一化平均 duty，[0, 1]。 */
    bool limited; /* 请求电压矢量超过本次 VBUS 对应的圆幅值上限。 */
    bool valid; /* false 时全部数值及 limited 清零；调用方必须拒绝下发。 */
} FocModulation;

/*
 * sin/cos 由调用方提供，不绑定三角函数、CORDIC 或硬件对象。
 * 二者必须有限，且 abs(sin^2 + cos^2 - 1) <= 0.001；不静默重新归一化。
 * d=alpha*cos+beta*sin，q=-alpha*sin+beta*cos。
 * 函数返回是否得到有效输出；任何输入或数值失败均清空非空 out。
 */
bool foc_park(float alpha, float beta, float sin_theta, float cos_theta, FocDq *out);

/* alpha=d*cos-q*sin，beta=d*sin+q*cos；失效策略与 Park 相同。 */
bool foc_inverse_park(float d, float q, float sin_theta, float cos_theta, FocAlphaBeta *out);

/*
 * 先保持方向将二维请求电压限幅到 max_voltage_ratio * vbus_v，再做居中 SVPWM。
 * vbus_v 必须有限且大于零，ratio 必须有限且处于 (0, 1/sqrt(3)]。
 * 对三相相电压减去最大/最小值的中点后，duty=0.5+phase/vbus。
 * 零矢量得到三相 0.5；此值不表示功率关闭。函数不执行硬件写入。
 * 不包含过调制、死区、采样窗口或最小脉宽补偿；装配方仍需约束占空比。
 * 输入非法或中间数值不可表示时，非空 out 的全部字段清零，返回 false。
 */
bool foc_svpwm(float alpha_v, float beta_v, float vbus_v, float max_voltage_ratio,
               FocModulation *out);

#endif
