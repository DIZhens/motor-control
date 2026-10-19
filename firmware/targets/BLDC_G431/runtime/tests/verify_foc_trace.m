function report = verify_foc_trace(csv_path, metadata_path, report_path)
% 用独立双精度连续 PMSM 方程复核离散轨迹；只使用基础 MATLAB 与 ode45。
% 每段只从首行取得初态，随后累计积分；不读取或复现任何控制器计算。
% 对象参数为合成数值参数，不代表真实 2804 电机或硬件验收。

assert(isfile(csv_path) && isfile(metadata_path), '找不到轨迹或参数文件。');
assert(~isfile(report_path), '报告已存在，拒绝覆盖。');
metadata = jsondecode(fileread(metadata_path));
assert(metadata.schema_version == 1, '不支持的轨迹格式版本。');
plant = metadata.plant;
dt = metadata.sample_interval_s;
values = [plant.resistance_ohm, plant.inductance_d_h, plant.inductance_q_h, ...
    plant.flux_linkage_wb, plant.inertia_kg_m2, plant.viscous_friction_nm_s, ...
    plant.pole_pairs, dt];
assert(all(isfinite(values)) && all(values([1:5, 7:8]) > 0) && values(6) >= 0 ...
    && plant.pole_pairs == floor(plant.pole_pairs), '合成对象参数非法。');

trace = readtable(csv_path, 'VariableNamingRule', 'preserve');
columns = {'time_s', 'applied_voltage_alpha_v', 'applied_voltage_beta_v', ...
    'true_id_a', 'true_iq_a', 'true_omega_rad_s', 'true_mechanical_turns', ...
    'load_torque_nm', 'state', 'target_velocity_rpm'};
assert(all(ismember(columns, trace.Properties.VariableNames)), '轨迹缺少必要列。');
data = trace{:, columns};
assert(size(data, 1) >= 3 && all(isfinite(data), 'all'), '轨迹长度不足或包含非有限数。');
assert(all(abs(diff(data(:, 1)) - dt) <= max(1e-12, dt * 1e-8)), ...
    '轨迹时间不是声明的连续固定步长。');

% 最多四段，每段至多 750 个连续 ZOH 区间，优先包含目标/状态变化。
window = min(750, size(data, 1) - 1);
last_start = size(data, 1) - window;
changes = find(diff(data(:, 10)) ~= 0 | diff(data(:, 9)) ~= 0) + 1;
starts = [1, last_start];
% 首尾段已覆盖起步与最终 Stop，优先把有限的额外区段分配给内部目标变化。
changes = changes(changes > window & changes < last_start);
if ~isempty(changes)
    selected = changes(unique(round(linspace(1, numel(changes), min(2, numel(changes))))));
    starts = [starts, max(1, min(last_start, selected(:)' - floor(window / 3)))];
end
starts = unique([starts, max(1, round(last_start / 3)), max(1, round(2 * last_start / 3))], 'stable');
starts = starts(1:min(4, numel(starts)));

% 阈值以实际单位给出；不是依据当前观测误差自动放宽的容差。
limits = [0.002, 0.002, 0.1, 5e-5]; % Id[A]、Iq[A]、机械速度[rad/s]、机械位置[turn]。
options = odeset('RelTol', 1e-9, 'AbsTol', [1e-10, 1e-10, 1e-10, 1e-12]);
segments = repmat(struct('first_row', 0, 'last_row', 0, 'start_time_s', 0, ...
    'end_time_s', 0, 'max_abs_error', [], 'rms_error', [], 'passed', false), 1, numel(starts));
overall = zeros(1, 4);
for section = 1:numel(starts)
    first = starts(section);
    final = first + window;
    state = [data(first, 4:6), data(first, 7) * 2 * pi]';
    errors = zeros(window, 4);
    for row = first:(final - 1)
        alpha = data(row, 2);
        beta = data(row, 3);
        load_torque = data(row, 8);
        [~, solution] = ode45(@(~, x) pmsm_derivative(x, alpha, beta, load_torque, plant), ...
            [0, dt], state, options);
        state = solution(end, :)';
        actual = [state(1:3)', state(4) / (2 * pi)];
        expected = [data(row + 1, 4:6), data(row + 1, 7)];
        errors(row - first + 1, :) = actual - expected;
    end
    maximum = max(abs(errors), [], 1);
    assert(all(isfinite(maximum)), '连续积分产生非有限数。');
    segments(section) = struct('first_row', first, 'last_row', final, ...
        'start_time_s', data(first, 1), 'end_time_s', data(final, 1), ...
        'max_abs_error', maximum, 'rms_error', sqrt(mean(errors .^ 2, 1)), ...
        'passed', all(maximum <= limits));
    overall = max(overall, maximum);
end

passed = all([segments.passed]);
report = struct('schema_version', 1, 'passed', passed, ...
    'scope', '合成连续 PMSM 数值对象；不证明真实2804参数、硬件采样时序或闭环稳定性', ...
    'method', '基础MATLAB ode45；每拍固定alpha/beta电压与负载，每段累计积分', ...
    'power_off_simplification', '关断段的零alpha/beta电压只代表理想零电压衰减，不模拟功率级实际开路或二极管行为', ...
    'csv_path', char(csv_path), 'metadata_path', char(metadata_path), ...
    'sample_interval_s', dt, 'plant', plant, ...
    'error_columns', {{'id_a', 'iq_a', 'mechanical_omega_rad_s', 'mechanical_turns'}}, ...
    'max_abs_error', overall, 'absolute_limits', limits, 'segments', segments);
if passed
    [report_directory, report_name] = fileparts(report_path);
    png_path = fullfile(report_directory, [report_name, '-trace.png']);
    draw_trace(trace, png_path);
    report.figure_path = png_path;
end
fid = fopen(report_path, 'w', 'n', 'UTF-8');
assert(fid ~= -1, '无法创建报告。');
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, '%s\n', jsonencode(report));
fprintf('连续 PMSM 独立复核：%d 段，最大误差 [%g %g %g %g]。\n', ...
    numel(starts), overall(1), overall(2), overall(3), overall(4));
assert(passed, '独立 ode45 累计状态误差超过预设阈值。');
end

function derivative = pmsm_derivative(state, alpha, beta, load_torque, plant)
% 标准凸极 PMSM 电压方程，速度为机械 rad/s，转矩采用幅值不变 Clarke 约定。
id = state(1);
iq = state(2);
omega = state(3);
theta_e = plant.pole_pairs * state(4);
vd = alpha * cos(theta_e) + beta * sin(theta_e);
vq = -alpha * sin(theta_e) + beta * cos(theta_e);
electrical_omega = plant.pole_pairs * omega;
torque = 1.5 * plant.pole_pairs * ...
    (plant.flux_linkage_wb * iq + (plant.inductance_d_h - plant.inductance_q_h) * id * iq);
derivative = [(vd - plant.resistance_ohm * id + electrical_omega * plant.inductance_q_h * iq) / plant.inductance_d_h; ...
    (vq - plant.resistance_ohm * iq - electrical_omega * (plant.inductance_d_h * id + plant.flux_linkage_wb)) / plant.inductance_q_h; ...
    (torque - load_torque - plant.viscous_friction_nm_s * omega) / plant.inertia_kg_m2; ...
    omega];
end

function draw_trace(trace, png_path)
% 仅在实际数值复核通过后导出图；整个过程不创建可见窗口。
assert(~isfile(png_path), '轨迹图已存在，拒绝覆盖。');
fig = figure('Visible', 'off', 'Color', 'white', 'Position', [100, 100, 1200, 900]);
cleanup = onCleanup(@() close(fig));
tiledlayout(fig, 3, 1, 'TileSpacing', 'compact');
names = trace.Properties.VariableNames;
time_s = trace.time_s;
nexttile;
plot(time_s, trace.target_velocity_rpm, '--', 'DisplayName', 'Target'); hold on;
plot(time_s, trace.true_omega_rad_s * 60 / (2 * pi), 'DisplayName', 'True');
if ismember('estimated_velocity_rpm', names)
    plot(time_s, trace.estimated_velocity_rpm, 'DisplayName', 'Estimated');
end
grid on; ylabel('Speed [rpm]'); legend('Location', 'best');
title('Synthetic PMSM numerical trace');
nexttile;
plot(time_s, trace.true_id_a, 'DisplayName', 'True Id'); hold on;
plot(time_s, trace.true_iq_a, 'DisplayName', 'True Iq');
for item = {'id_reference_a', 'iq_reference_a'}
    if ismember(item{1}, names)
        plot(time_s, trace.(item{1}), '--', 'DisplayName', strrep(item{1}, '_', ' '));
    end
end
grid on; ylabel('Current [A]'); legend('Location', 'best');
nexttile; hold on;
has_duty = all(ismember({'active_duty_a', 'active_duty_b', 'active_duty_c'}, names));
if has_duty
    for phase = {'a', 'b', 'c'}
        name = ['active_duty_', phase{1}];
        plot(time_s, trace.(name), 'DisplayName', ['Duty ', upper(phase{1})]);
    end
    ylabel('Duty [1]'); yyaxis right;
end
stairs(time_s, trace.state, 'DisplayName', 'State');
if ismember('power_enabled', names)
    stairs(time_s, trace.power_enabled, '--', 'DisplayName', 'Power enabled');
end
grid on; xlabel('Time [s]'); ylabel('State / power [1]'); legend('Location', 'best');
exportgraphics(fig, png_path, 'Resolution', 160);
end
