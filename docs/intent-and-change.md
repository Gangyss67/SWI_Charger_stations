# Project Frame

## Reservation domain
Charging stations on a parking lot, reserved in 30-minute time slots.

## Purpose
The system is used to reserve charging stations with different charging speeds on a parking lot. The goal is to prevent situations where the parking lot is overcrowded or the chargers are being used inefficiently.

## Users / Stakeholders
- User – driver of an EV or a plug-in hybrid; can check availability, create, submit and cancel their own reservations
- Admin – can view all reservations, approve (confirm) and cancel them

## Core concepts
- Reservation – a user's claim on one charging station for a time interval made of 30-minute slots
- Charging station (Resource) – one of 10 chargers with a given power in kW; 3 of them are high-speed
- User – a person with an account and a vehicle type
- Vehicle type – EV (BEV) or plug-in hybrid (PHEV); decides which stations the user may reserve

## Core operations
- Create reservation
- Confirm / approve reservation
- Cancel reservation
- Check availability

## Persistent state
- Reservation: user, charging station, start time, end time (30-minute slots), state, created/updated timestamps
- Charging station: name, charging power (kW), whether it is high-speed
- User: username, password hash, vehicle type (EV / plug-in hybrid), admin role

## State-changing operation
- DRAFT → PENDING (user submits the reservation for approval)
- PENDING → CONFIRMED (admin approves)
- DRAFT / PENDING / CONFIRMED → CANCELLED (user cancels their own reservation, or admin cancels any)

## Common business rule
Confirmed reservations for the same charging station must not overlap.

## Domain-specific business rule
Only an EV can park at a high-speed charging station, plug-in hybrids have to use slower chargers.

## External / system boundary
Notification Service – a parking space has opened up, a parking space with a higher charging speed has opened up

## Assumption
User discipline. Users arrive at the parking space and leave it on time – complying with reserved time slots

## Unknown
How the actual presence and identity (EV type and license plate) of the vehicle at the location will be verified.


## Selected future pressure
Category: Q

Concrete pressure:
A 10x increase in concurrent requests to reserve available slots and make reservations within a narrow time window.

Why it is relevant to our reservation system:
Confirmed reservations for the same station must not overlap. High levels of concurrent activity create a risk of race conditions (where two users attempt to book the same time slot at the same station within the same fraction of a second). The system must atomically verify both the availability of the slot and the fact that the fast chargers may only be occupied by EVs and not plug-in hybrids.
