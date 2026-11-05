<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Order extends Model
{
    protected $fillable = ['customer_id', 'total'];

    public function total(array $items): int
    {
        return array_sum(array_column($items, 'price'));
    }
}
