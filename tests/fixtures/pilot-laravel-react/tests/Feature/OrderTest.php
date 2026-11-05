<?php

namespace Tests\Feature;

use App\Models\Order;
use PHPUnit\Framework\TestCase;

class OrderTest extends TestCase
{
    public function test_total_sums_prices(): void
    {
        $this->assertSame(5, (new Order())->total([['price' => 2], ['price' => 3]]));
    }
}
