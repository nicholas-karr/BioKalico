// Commands for controlling GPIO output pins
//
// Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // struct gpio_out
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_is_before
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_add_timer

struct digital_out_s {
    struct timer timer;
    uint32_t on_duration, off_duration, end_time;
    struct gpio_out pin;
    uint32_t max_duration, cycle_time;
    struct move_queue_head mq;
    uint8_t flags;
};

struct digital_move {
    struct move_node node;
    uint32_t waketime, on_duration;
};

enum {
    DF_ON=1<<0, DF_TOGGLING=1<<1, DF_CHECK_END=1<<2, DF_DEFAULT_ON=1<<4
};

struct pulse_out_s {
    struct timer timer;
    struct gpio_out pin;
    uint32_t pulse_width, off_duration, remaining;
    uint8_t flags;
    uint8_t pulse_value, default_value;
};

enum {
    PF_ACTIVE=1<<0, PF_PIN_ON=1<<1, PF_INFINITE=1<<2
};

static uint_fast8_t digital_load_event(struct timer *timer);

// Scheduled pulse train event
static uint_fast8_t
pulse_event(struct timer *timer)
{
    struct pulse_out_s *p = container_of(timer, struct pulse_out_s, timer);
    uint8_t flags = p->flags;
    if (!(flags & PF_ACTIVE))
        return SF_DONE;
    if (!(flags & PF_PIN_ON)) {
        gpio_out_write(p->pin, p->pulse_value);
        p->flags = flags | PF_PIN_ON;
        p->timer.waketime += p->pulse_width;
        return SF_RESCHEDULE;
    }

    gpio_out_write(p->pin, p->default_value);
    flags &= ~PF_PIN_ON;
    if (!(flags & PF_INFINITE)) {
        uint32_t remaining = p->remaining;
        if (!remaining) {
            p->flags = flags & ~PF_ACTIVE;
            return SF_DONE;
        }
        remaining--;
        p->remaining = remaining;
        if (!remaining) {
            p->flags = flags & ~PF_ACTIVE;
            return SF_DONE;
        }
    }
    p->flags = flags;
    p->timer.waketime += p->off_duration;
    return SF_RESCHEDULE;
}

// Software PWM toggle event
static uint_fast8_t
digital_toggle_event(struct timer *timer)
{
    struct digital_out_s *d = container_of(timer, struct digital_out_s, timer);
    gpio_out_toggle_noirq(d->pin);
    d->flags ^= DF_ON;
    uint32_t waketime = d->timer.waketime;
    if (d->flags & DF_ON)
        waketime += d->on_duration;
    else
        waketime += d->off_duration;
    if (d->flags & DF_CHECK_END && !timer_is_before(waketime, d->end_time)) {
        // End of normal pulsing - next event loads new pwm settings
        d->timer.func = digital_load_event;
        waketime = d->end_time;
    }
    d->timer.waketime = waketime;
    return SF_RESCHEDULE;
}

// Load next pin output setting
static uint_fast8_t
digital_load_event(struct timer *timer)
{
    // Apply next update and remove it from queue
    struct digital_out_s *d = container_of(timer, struct digital_out_s, timer);
    if (move_queue_empty(&d->mq))
        shutdown("Missed scheduling of next digital out event");
    struct move_node *mn = move_queue_pop(&d->mq);
    struct digital_move *m = container_of(mn, struct digital_move, node);
    uint32_t on_duration = m->on_duration;
    uint8_t flags = on_duration ? DF_ON : 0;
    gpio_out_write(d->pin, flags);
    move_free(m);

    // Calculate next end_time and flags
    uint32_t end_time = 0;
    if (!flags || on_duration >= d->cycle_time) {
        // Pin is in an always on or always off state
        if (!flags != !(d->flags & DF_DEFAULT_ON) && d->max_duration) {
            end_time = d->timer.waketime + d->max_duration;
            flags |= DF_CHECK_END;
        }
    } else {
        flags |= DF_TOGGLING;
        if (d->max_duration) {
            end_time = d->timer.waketime + d->max_duration;
            flags |= DF_CHECK_END;
        }
    }
    if (!move_queue_empty(&d->mq)) {
        struct move_node *nn = move_queue_first(&d->mq);
        uint32_t wake = container_of(nn, struct digital_move, node)->waketime;
        if (flags & DF_CHECK_END && timer_is_before(end_time, wake))
            shutdown("Scheduled digital out event will exceed max_duration");
        end_time = wake;
        flags |= DF_CHECK_END;
    }
    d->end_time = end_time;
    d->flags = flags | (d->flags & DF_DEFAULT_ON);

    // Schedule next event
    if (!(flags & DF_TOGGLING)) {
        if (!(flags & DF_CHECK_END))
            // Pin not toggling and nothing scheduled
            return SF_DONE;
        d->timer.waketime = end_time;
        return SF_RESCHEDULE;
    }
    uint32_t waketime = d->timer.waketime + on_duration;
    if (flags & DF_CHECK_END && !timer_is_before(waketime, end_time)) {
        d->timer.waketime = end_time;
        return SF_RESCHEDULE;
    }
    d->timer.func = digital_toggle_event;
    d->timer.waketime = waketime;
    d->on_duration = on_duration;
    d->off_duration = d->cycle_time - on_duration;
    return SF_RESCHEDULE;
}

void
command_config_digital_out(uint32_t *args)
{
    struct gpio_out pin = gpio_out_setup(args[1], !!args[2]);
    struct digital_out_s *d = oid_alloc(args[0], command_config_digital_out
                                        , sizeof(*d));
    d->pin = pin;
    d->flags = (args[2] ? DF_ON : 0) | (args[3] ? DF_DEFAULT_ON : 0);
    d->max_duration = args[4];
    move_queue_setup(&d->mq, sizeof(struct digital_move));
}
DECL_COMMAND(command_config_digital_out,
             "config_digital_out oid=%c pin=%u value=%c"
             " default_value=%c max_duration=%u");

void
command_set_digital_out_pwm_cycle(uint32_t *args)
{
    struct digital_out_s *d = oid_lookup(args[0], command_config_digital_out);
    irq_disable();
    if (!move_queue_empty(&d->mq))
        shutdown("Can not set soft pwm cycle ticks while updates pending");
    d->cycle_time = args[1];
    irq_enable();
}
DECL_COMMAND(command_set_digital_out_pwm_cycle,
             "set_digital_out_pwm_cycle oid=%c cycle_ticks=%u");

void
command_queue_digital_out(uint32_t *args)
{
    struct digital_out_s *d = oid_lookup(args[0], command_config_digital_out);
    struct digital_move *m = move_alloc();
    uint32_t time = m->waketime = args[1];
    m->on_duration = args[2];

    irq_disable();
    int first_on_queue = move_queue_push(&m->node, &d->mq);
    if (!first_on_queue) {
        irq_enable();
        return;
    }
    uint8_t flags = d->flags;
    if (flags & DF_CHECK_END && timer_is_before(d->end_time, time))
        shutdown("Scheduled digital out event will exceed max_duration");
    d->end_time = time;
    d->flags = flags | DF_CHECK_END;
    if (flags & DF_TOGGLING && timer_is_before(d->timer.waketime, time)) {
        // digital_toggle_event() will schedule a load event when ready
    } else {
        // Schedule the loading of the parameters at the requested time
        sched_del_timer(&d->timer);
        d->timer.waketime = time;
        d->timer.func = digital_load_event;
        sched_add_timer(&d->timer);
    }
    irq_enable();
}
DECL_COMMAND(command_queue_digital_out,
             "queue_digital_out oid=%c clock=%u on_ticks=%u");

void
command_config_pulse_out(uint32_t *args)
{
    struct gpio_out pin = gpio_out_setup(args[1], !!args[3]);
    struct pulse_out_s *p = oid_alloc(args[0], command_config_pulse_out
                                      , sizeof(*p));
    p->pin = pin;
    p->pulse_value = !!args[2];
    p->default_value = !!args[3];
    p->flags = 0;
}
DECL_COMMAND(command_config_pulse_out,
             "config_pulse_out oid=%c pin=%u value=%c default_value=%c");

void
command_start_pulse_out(uint32_t *args)
{
    struct pulse_out_s *p = oid_lookup(args[0], command_config_pulse_out);
    uint32_t pulse_width = args[2], period = args[3], count = args[4];
    if (pulse_width < timer_from_us(100))
        shutdown("Pulse width below minimum");
    if (period <= pulse_width)
        shutdown("Pulse period must be greater than pulse width");
    if (pulse_width > period / 10)
        shutdown("Pulse duty cycle exceeds 10%");

    irq_disable();
    sched_del_timer(&p->timer);
    gpio_out_write(p->pin, p->default_value);
    p->pulse_width = pulse_width;
    p->off_duration = period - pulse_width;
    p->remaining = count;
    p->flags = PF_ACTIVE | (count ? 0 : PF_INFINITE);
    p->timer.waketime = args[1];
    p->timer.func = pulse_event;
    sched_add_timer(&p->timer);
    irq_enable();
}
DECL_COMMAND(command_start_pulse_out,
             "start_pulse_out oid=%c clock=%u width_ticks=%u"
             " period_ticks=%u count=%u");

void
command_stop_pulse_out(uint32_t *args)
{
    struct pulse_out_s *p = oid_lookup(args[0], command_config_pulse_out);
    irq_disable();
    sched_del_timer(&p->timer);
    p->flags = 0;
    p->remaining = 0;
    gpio_out_write(p->pin, p->default_value);
    irq_enable();
}
DECL_COMMAND(command_stop_pulse_out, "stop_pulse_out oid=%c");

void
command_update_digital_out(uint32_t *args)
{
    struct digital_out_s *d = oid_lookup(args[0], command_config_digital_out);
    sched_del_timer(&d->timer);
    if (!move_queue_empty(&d->mq))
        shutdown("update_digital_out not valid with active queue");
    uint8_t value = args[1], flags = d->flags, on_flag = value ? DF_ON : 0;
    gpio_out_write(d->pin, on_flag);
    if (!on_flag != !(flags & DF_DEFAULT_ON) && d->max_duration) {
        d->timer.waketime = d->end_time = timer_read_time() + d->max_duration;
        d->timer.func = digital_load_event;
        d->flags = (flags & DF_DEFAULT_ON) | on_flag | DF_CHECK_END;
        sched_add_timer(&d->timer);
    } else {
        d->flags = (flags & DF_DEFAULT_ON) | on_flag;
    }
}
DECL_COMMAND(command_update_digital_out, "update_digital_out oid=%c value=%c");

void
digital_out_shutdown(void)
{
    uint8_t i;
    struct digital_out_s *d;
    foreach_oid(i, d, command_config_digital_out) {
        gpio_out_write(d->pin, d->flags & DF_DEFAULT_ON);
        d->flags = d->flags & DF_DEFAULT_ON ? DF_ON | DF_DEFAULT_ON : 0;
        move_queue_clear(&d->mq);
    }
}
DECL_SHUTDOWN(digital_out_shutdown);

void
pulse_out_shutdown(void)
{
    uint8_t i;
    struct pulse_out_s *p;
    foreach_oid(i, p, command_config_pulse_out) {
        gpio_out_write(p->pin, p->default_value);
        p->flags = 0;
        p->remaining = 0;
    }
}
DECL_SHUTDOWN(pulse_out_shutdown);

void
command_set_digital_out(uint32_t *args)
{
    gpio_out_setup(args[0], args[1]);
}
DECL_COMMAND(command_set_digital_out, "set_digital_out pin=%u value=%c");
