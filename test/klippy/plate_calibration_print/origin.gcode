; Printed by plate_calibration_print.test
G90
G1 Z5 F600
ASSERT TEST='{printer.gcode_move.homing_origin.z == 0}'
SELECT_TOOLHEAD TOOLHEAD=pla
ASSERT TEST='{printer.plate_calibration.origin_tool == "pla"}'
ASSERT TEST='{printer.gcode_move.homing_origin.x == 12}'
ASSERT TEST='{printer.gcode_move.homing_origin.z == 2}'
G1 X1 Y1 Z1
M400
ASSERT TEST='{printer.toolhead.position.x == 13}'
ASSERT TEST='{printer.toolhead.position.z == 3}'
; A head without a saved position prints in machine coordinates
SELECT_TOOLHEAD TOOLHEAD=gel
ASSERT TEST='{printer.plate_calibration.origin_tool == none}'
ASSERT TEST='{printer.gcode_move.homing_origin.z == 0}'
SELECT_TOOLHEAD TOOLHEAD=pla
; Homing is done in machine coordinates
G28 Z
ASSERT TEST='{printer.gcode_move.homing_origin.z == 0}'
SELECT_TOOLHEAD TOOLHEAD=pla
ASSERT TEST='{printer.gcode_move.homing_origin.z == 2}'
