#ifndef G431_TARGET_CORE_SERVER_H
#define G431_TARGET_CORE_SERVER_H

/* Bootstrap在Transport开放RX之前调用；service仅由Communication任务调用。 */
void target_core_server_start(void);
void target_core_server_service(void);

#endif
